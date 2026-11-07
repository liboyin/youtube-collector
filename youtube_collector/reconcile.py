"""The reconciliation thread: keep the status table in step with staging (DESIGN 5.8, 5.13, 5.14).

SMB mounts deliver no change notifications, so a pass checks whether the files each done or failed
item recorded still exist, removes items whose files are all gone, removes finished jobs left with
nothing (decision C26), and prunes old tombstones. Passes run periodically and on request. The API
never touches the share: it asks this thread for a pass, waits a bounded time, and reads the cached
results (decision C25). Only this thread probes staging for reconciliation, so at most one probe is
outstanding however many requests arrive.
"""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from youtube_collector import staging
from youtube_collector.store import Store

__all__ = ['PassResult', 'Reconciler', 'StagingCheck']

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StagingCheck:
    """The most recent check of the staging marker, for ``/health`` (DESIGN 5.14).

    Attributes:
        marker_present: Whether the marker was present, and so whether work may proceed.
        checked_at: When the check finished, on the reconciler's monotonic clock.
    """

    marker_present: bool
    checked_at: float


@dataclass(frozen=True)
class PassResult:
    """What one pass did.

    Attributes:
        reconciled: False when the pass stopped because the staging area was unavailable.
        removed_items: How many items it removed.
        removed_jobs: How many jobs it removed.
        pruned: How many tombstones it pruned.
    """

    reconciled: bool
    removed_items: int = 0
    removed_jobs: int = 0
    pruned: int = 0


class Reconciler:
    """The reconciliation thread and the cached results the API reads.

    Args:
        open_store: Opens the thread's own store; called on the reconciliation thread.
        staging_root: The staging area's root directory.
        notify: Wakes long-poll waiters after records change.
        clock: Wall-clock epoch seconds, for the time of the last completed reconciliation.
        monotonic: A monotonic clock, for the age of the cached staging check.
        interval: Seconds between periodic passes (DESIGN section 7: 30 by default).
        tombstone_retention: Seconds tombstones are kept before pruning (7 days by default).
    """

    def __init__(
        self, *, open_store: Callable[[], Store], staging_root: Path, notify: Callable[[], None],
        clock: Callable[[], float], monotonic: Callable[[], float], interval: float = 30.0,
        tombstone_retention: float = 7 * 24 * 60 * 60,
    ) -> None:
        """Store the collaborators; nothing runs until ``run``."""
        self._open_store = open_store
        self._root = staging_root
        self._notify = notify
        self._clock = clock
        self._monotonic = monotonic
        self._interval = interval
        self._retention = tombstone_retention
        self._condition = threading.Condition()
        self._started = 0
        self._completed = 0
        self._reconciled_at: float | None = None
        self._staging: StagingCheck | None = None
        self._paused = False
        self._stopping = threading.Event()
        self._wakeup = threading.Event()

    # Read by the API, from any thread, without touching the share.

    def last_reconciled_at(self) -> float | None:
        """Return when the last pass that reconciled finished, as epoch seconds, or None before any."""
        with self._condition:
            return self._reconciled_at

    def staging_check(self) -> StagingCheck | None:
        """Return the most recent staging marker check, or None before the first."""
        with self._condition:
            return self._staging

    def request_pass(self) -> int:
        """Ask for a pass that starts after now, and return a ticket for ``wait_for_pass``.

        Requests made while a pass is waiting to start share it, so requests never queue up probes.
        """
        with self._condition:
            ticket = self._started + 1
        self._wakeup.set()
        return ticket

    def wait_for_pass(self, ticket: int, timeout: float) -> bool:
        """Wait up to ``timeout`` seconds for the pass a ticket names to finish.

        Returns:
            True when it finished, False on timeout, for example while the share is stalled.
        """
        with self._condition:
            return self._condition.wait_for(lambda: self._completed >= ticket, timeout)

    # The thread.

    def stop(self) -> None:
        """Ask the thread to stop after its current pass."""
        self._stopping.set()
        self._wakeup.set()

    def run(self) -> None:
        """Run passes until ``stop``: one at start, then every ``interval`` or on request.

        An unexpected error in a pass is logged and the loop continues; waiters are still released.
        """
        with self._open_store() as store:
            while not self._stopping.is_set():
                try:
                    self.run_pass(store)
                except Exception:
                    logger.exception('Reconciliation pass failed')
                self._wakeup.wait(self._interval)
                self._wakeup.clear()

    def run_pass(self, store: Store) -> PassResult:
        """Run one pass and release the waiters whose tickets it covers.

        The marker is checked before probing and again after, before any record is removed: an
        unmounted share looks like an empty directory, so a pass that lost the share part-way
        changes nothing (DESIGN 5.13). Files that cannot be checked keep their items.
        """
        with self._condition:
            self._started += 1
        try:
            result = self._reconcile(store)
        finally:
            with self._condition:
                self._completed = self._started
                self._condition.notify_all()
        return result

    def _reconcile(self, store: Store) -> PassResult:
        """Probe staging and apply removals; see ``run_pass``."""
        if not self._check_marker():
            return PassResult(reconciled=False)
        gone: list[str] = []
        for item in store.finished_items():
            try:
                present = any(staging.file_present(self._root / path) for path in item.paths)
            except OSError as error:
                logger.warning('Cannot check files of item %s; keeping it: %s', item.item_id, error)
                continue
            if not present:
                gone.append(item.item_id)
        if not self._check_marker():
            return PassResult(reconciled=False)
        # Each write commits on its own, so a later failure must not swallow the notification for an
        # earlier one that already committed.
        removed_items = removed_jobs = pruned = 0
        try:
            removed_items = store.remove_items(gone)
            removed_jobs = store.remove_finished_jobs()
            pruned = store.prune(self._retention)
        finally:
            if removed_items or removed_jobs or pruned:
                self._notify()
        with self._condition:
            self._reconciled_at = self._clock()
        return PassResult(reconciled=True, removed_items=removed_items, removed_jobs=removed_jobs, pruned=pruned)

    def _check_marker(self) -> bool:
        """Check the marker, cache the result for health, and log pausing and resuming once each."""
        present = staging.marker_present(self._root)
        with self._condition:
            self._staging = StagingCheck(present, self._monotonic())
        if not present and not self._paused:
            logger.warning('Staging marker %s is missing; reconciliation paused', self._root / staging.MARKER_NAME)
        elif present and self._paused:
            logger.warning('Staging marker is present again; reconciliation resumed')
        self._paused = not present
        return present
