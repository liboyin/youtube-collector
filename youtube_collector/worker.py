"""The YouTube worker thread: round-robin scheduling and item processing (DESIGN 5.3, 5.4, 5.7, 5.12-5.14).

All YouTube traffic runs on this one thread, one request at a time. The worker takes the next unit
of work in round-robin order (decision C9), expands playlists, checks each artifact with the
``NEVER`` policy before any network work, downloads, publishes through a stage directory, and
records the item's outcome with its failures in one store write. It pauses without changing any
record while the staging marker is missing. It is the only thread that touches the staging share,
and the latest marker check it made is what ``/health`` reports (decisions C25 and C43).
"""

import enum
import logging
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import requests
from youtube_transcript_api import YouTubeTranscriptApiException

from youtube_collector import staging
from youtube_collector.categories import categorize, failure_message
from youtube_collector.store import (
    CaptionsOutcome,
    ItemResult,
    ItemState,
    MediaOutcome,
    NewFailure,
    Phase,
    PlaylistEntry,
    Store,
    TargetType,
    TrackKind,
    WorkItem,
    WorkUnit,
)
from youtube_collector.urls import PlaylistTarget, VideoTarget
from youtube_collector.youtube import ActivityCallback, Playlist, VideoInfo

__all__ = ['CaptionSource', 'MediaSource', 'StagingCheck', 'StagingUnavailable', 'Step', 'Track', 'Worker']

logger = logging.getLogger(__name__)

# Caption-service errors (DESIGN 5.7): any youtube-transcript-api exception, or any requests
# exception, including timeouts. Other caption errors fail the item.
_CAPTION_SERVICE_ERRORS = (YouTubeTranscriptApiException, requests.RequestException)


class Track(Protocol):
    """A caption track as the caption adapter returns it."""

    @property
    def language_code(self) -> str:
        """The track's language code."""
        ...

    @property
    def is_generated(self) -> bool:
        """Whether YouTube generated the track."""
        ...


class MediaSource(Protocol):
    """The yt-dlp adapter's interface (``youtube.YoutubeClient``)."""

    def expand_playlist(self, url: str) -> Playlist:
        """List a playlist's entries."""
        ...

    def video_info(self, url: str) -> VideoInfo:
        """Extract a video's metadata."""
        ...

    def download_media(self, video: VideoInfo, path: Path, on_activity: ActivityCallback) -> None:
        """Download a video as MP4 to ``path``."""
        ...


class CaptionSource[T: Track](Protocol):
    """The caption adapter's interface (``captions.CaptionClient``)."""

    def find_track(self, video_id: str, languages: Sequence[str]) -> T | None:
        """Return the caption track to use, or None."""
        ...

    def fetch_srt(self, track: T) -> str:
        """Fetch a track as SRT."""
        ...


@dataclass(frozen=True)
class StagingCheck:
    """The most recent check of the staging marker, for ``/health`` (DESIGN 5.14).

    Attributes:
        marker_present: Whether the marker was present, and so whether work may proceed.
        checked_at: When the check finished, on the worker's monotonic clock.
    """

    marker_present: bool
    checked_at: float


class StagingUnavailable(OSError):
    """Raised when the staging marker disappears before a publication."""


class Step(enum.Enum):
    """What one worker step did."""

    PAUSED = 'paused'
    IDLE = 'idle'
    EXPANDED = 'expanded'
    PROCESSED = 'processed'


class _ItemFailed(Exception):
    """Ends an item early with a failure for the given phase; carries the error as its cause."""

    def __init__(self, phase: Phase, error: BaseException) -> None:
        """Remember the phase and the error."""
        super().__init__(phase, error)
        self.failure = NewFailure(phase, categorize(error), failure_message(error))


@dataclass
class _Outcome:
    """The item's result as it is assembled."""

    title: str | None
    media_outcome: MediaOutcome | None = None
    media_path: str | None = None
    captions_outcome: CaptionsOutcome | None = None
    captions_path: str | None = None
    track_code: str | None = None
    track_kind: TrackKind | None = None
    failures: list[NewFailure] = field(default_factory=list)

    def result(self, state: ItemState) -> ItemResult:
        """Freeze the outcome as the store's item result."""
        return ItemResult(
            state=state, title=self.title, media_outcome=self.media_outcome, media_path=self.media_path,
            captions_outcome=self.captions_outcome, captions_path=self.captions_path, track_code=self.track_code,
            track_kind=self.track_kind, failures=tuple(self.failures),
        )


class Worker[T: Track]:
    """The single thread that talks to YouTube.

    Args:
        open_store: Opens the worker's own store; called on the worker thread.
        staging_root: The staging area's root directory.
        media: The yt-dlp adapter.
        captions: The caption adapter.
        monotonic: A monotonic clock in seconds, for the age of the staging check.
        marker_check_interval: The minimum seconds between marker checks during a download (decision C43).
        idle_wait: How long to wait for a wake-up when there is no work.
        pause_wait: How long to wait before checking the staging marker again while paused.
        error_wait: How long to wait after an unexpected error before trying again.
        on_stopped: Called on the worker thread when ``run`` ends, however it ends, to release what the
            adapters hold. An abandoned worker calls it only when it finally finishes, so nothing it
            still uses can be released under it.
    """

    def __init__(
        self, *, open_store: Callable[[], Store], staging_root: Path, media: MediaSource, captions: CaptionSource[T],
        monotonic: Callable[[], float], marker_check_interval: float = 30.0,
        idle_wait: float = 30.0, pause_wait: float = 5.0, error_wait: float = 5.0,
        on_stopped: Callable[[], None] = lambda: None,
    ) -> None:
        """Store the collaborators; nothing runs until ``run``."""
        self._open_store = open_store
        self._root = staging_root
        self._media = media
        self._captions = captions
        self._monotonic = monotonic
        self._marker_check_interval = marker_check_interval
        self._idle_wait = idle_wait
        self._pause_wait = pause_wait
        self._error_wait = error_wait
        self._on_stopped = on_stopped
        self._cursor = 0
        self._paused = False
        self._staging_check: StagingCheck | None = None
        self._stopping = threading.Event()
        self._wakeup = threading.Event()

    @property
    def paused(self) -> bool:
        """Whether the worker found the staging marker missing at its last check."""
        return self._paused

    def staging_check(self) -> StagingCheck | None:
        """Return the latest marker check, or None before the first; safe from any thread (decision C43)."""
        return self._staging_check

    def wake(self) -> None:
        """Wake the worker early, for example after a submission; safe from any thread."""
        self._wakeup.set()

    def stop(self) -> None:
        """Ask the worker to stop taking units; the current one finishes first (decision C36)."""
        self._stopping.set()
        self._wakeup.set()

    def run(self) -> None:
        """Run the worker loop until ``stop``: recover interrupted work, then take units one by one.

        Each step's unexpected error is logged and retried after ``error_wait``, so one bad unit
        cannot kill the thread; the unit it interrupted is recovered at the next restart. ``on_stopped``
        runs last, also if opening the store or recovery raises.
        """
        try:
            with self._open_store() as store:
                store.recover()
                while not self._stopping.is_set():
                    try:
                        step = self.step(store)
                    except Exception:
                        logger.exception('Worker step failed')
                        self._wait(self._error_wait)
                        continue
                    if step is Step.PAUSED:
                        self._wait(self._pause_wait)
                    elif step is Step.IDLE:
                        self._wait(self._idle_wait)
        finally:
            self._on_stopped()

    def _wait(self, timeout: float) -> None:
        """Wait for a wake-up, a stop or the timeout."""
        self._wakeup.wait(timeout)
        self._wakeup.clear()

    def step(self, store: Store) -> Step:
        """Take and process at most one unit of work.

        Nothing starts while the staging marker is missing, and no record changes (DESIGN 5.13).
        """
        if not self._check_marker():
            if not self._paused:
                logger.warning('Staging marker %s is missing; pausing work', self._root / staging.MARKER_NAME)
                self._paused = True
            return Step.PAUSED
        if self._paused:
            logger.warning('Staging marker is present again; resuming work')
            self._paused = False
        unit = store.next_work(self._cursor)
        if unit is None:
            return Step.IDLE
        self._cursor = unit.job_seq
        if unit.item is None:
            self._expand(store, unit)
            return Step.EXPANDED
        self._process(store, unit, unit.item)
        return Step.PROCESSED

    def _expand(self, store: Store, unit: WorkUnit) -> None:
        """Expand a playlist job into items, or finish it with a failure (DESIGN 5.3, 5.7)."""
        store.start_expansion(unit.job_id)
        try:
            playlist = self._media.expand_playlist(PlaylistTarget(unit.target_id).url)
        except Exception as error:
            logger.warning('Expanding playlist %s failed: %s', unit.target_id, error)
            store.fail_expansion(unit.job_id, NewFailure(Phase.EXPANDING, categorize(error), failure_message(error)))
        else:
            entries = [PlaylistEntry(video.video_id, video.title) for video in playlist.entries]
            store.complete_expansion(unit.job_id, playlist.title, entries)

    def _process(self, store: Store, unit: WorkUnit, item: WorkItem) -> None:
        """Process one item and record its result with its failures in one write."""
        store.start_item(item.id)
        outcome = _Outcome(title=item.title)
        try:
            self._run_item(unit, item, outcome)
        except _ItemFailed as failed:
            logger.warning('Item %s (%s) failed: %s', item.id, item.video_id, failed.failure.message)
            outcome.failures.append(failed.failure)
            state = ItemState.FAILED
        else:
            state = ItemState.DONE
        store.finish_item(item.id, outcome.result(state))

    def _run_item(self, unit: WorkUnit, item: WorkItem, outcome: _Outcome) -> None:
        """Produce an item's artifacts into ``outcome``, raising ``_ItemFailed`` when the item fails.

        Each artifact is checked with ``NEVER`` before any network work (DESIGN 5.4). Media comes
        first; if it fails, captions are not attempted (DESIGN 5.7).
        """
        primary = Phase.MEDIA if unit.kind.wants_media else Phase.CAPTIONS
        try:
            directory = self._directory(unit)
            # MP4s are looked up for every kind: new captions pair with a single existing MP4 (C33).
            media = staging.find_artifacts(directory, item.video_id, staging.MEDIA_SUFFIX)
            captions = (
                staging.find_artifacts(directory, item.video_id, staging.CAPTIONS_SUFFIX)
                if unit.kind.wants_captions else []
            )
        except (OSError, ValueError) as error:
            raise _ItemFailed(primary, error) from error
        need_media = unit.kind.wants_media and not media
        need_captions = unit.kind.wants_captions and not captions
        if unit.kind.wants_media and media:
            outcome.media_outcome = MediaOutcome.EXISTING
            outcome.media_path = staging.relative_to_root(self._root, media[0])
        if captions:
            outcome.captions_outcome = CaptionsOutcome.EXISTING
            outcome.captions_path = staging.relative_to_root(self._root, captions[0])
        video = None
        # Media needs the extraction to download; captions need a title only when no single MP4
        # supplies their name (decision C33) and the playlist listing gave none.
        if need_media or (need_captions and len(media) != 1 and item.title is None):
            try:
                video = self._media.video_info(VideoTarget(item.video_id).url)
            except Exception as error:
                raise _ItemFailed(Phase.METADATA, error) from error
            outcome.title = video.title
        if need_media:
            assert video is not None  # extracted above whenever media is needed
            media = [self._download_media(item, directory, video, outcome)]
        if need_captions:
            self._fetch_captions(unit, item, directory, media, outcome)

    def _directory(self, unit: WorkUnit) -> Path:
        """Return the job's destination: the staging root, or its playlist folder (DESIGN 5.4).

        Raises:
            ValueError: If the playlist title leaves no room in the folder name.
        """
        if unit.target_type is TargetType.VIDEO:
            return self._root
        return self._root / staging.playlist_dir_name(unit.playlist_title or unit.target_id, unit.target_id)

    def _download_media(self, item: WorkItem, directory: Path, video: VideoInfo, outcome: _Outcome) -> Path:
        """Download and publish the MP4, recording its outcome; return its path."""
        try:
            target = directory / staging.media_name(outcome.title or '', item.video_id)

            def write(path: Path) -> None:
                self._media.download_media(video, path, self._marker_heartbeat())
                self._require_marker()

            published = staging.publish(target, write)
        except Exception as error:
            outcome.media_outcome = MediaOutcome.FAILED
            raise _ItemFailed(Phase.MEDIA, error) from error
        outcome.media_outcome = MediaOutcome.DOWNLOADED if published is not None else MediaOutcome.EXISTING
        outcome.media_path = staging.relative_to_root(self._root, target)
        return target

    def _fetch_captions(
        self, unit: WorkUnit, item: WorkItem, directory: Path, media: list[Path], outcome: _Outcome,
    ) -> None:
        """Fetch and publish captions, recording their outcome (DESIGN 5.6, 5.7).

        A caption-service error in a ``videos-with-captions`` item gives ``error`` with a failure
        record and leaves the item done, because its media succeeded; in a ``captions`` item it
        fails the item. Any other error fails the item.
        """
        try:
            track = self._captions.find_track(item.video_id, unit.languages or ())
            if track is None:
                outcome.captions_outcome = CaptionsOutcome.UNAVAILABLE
                return
            srt = self._captions.fetch_srt(track)
            target = directory / staging.captions_name_for(outcome.title or '', item.video_id, media)

            def write(path: Path) -> None:
                path.write_text(srt, encoding='utf-8')
                self._require_marker()

            published = staging.publish(target, write)
        except _CAPTION_SERVICE_ERRORS as error:
            if unit.kind.wants_media:
                outcome.captions_outcome = CaptionsOutcome.ERROR
                outcome.failures.append(NewFailure(Phase.CAPTIONS, categorize(error), failure_message(error)))
                logger.warning('Captions for %s failed: %s', item.video_id, error)
                return
            outcome.captions_outcome = CaptionsOutcome.FAILED
            raise _ItemFailed(Phase.CAPTIONS, error) from error
        except Exception as error:
            outcome.captions_outcome = CaptionsOutcome.FAILED
            raise _ItemFailed(Phase.CAPTIONS, error) from error
        outcome.captions_outcome = CaptionsOutcome.DOWNLOADED if published is not None else CaptionsOutcome.EXISTING
        outcome.captions_path = staging.relative_to_root(self._root, target)
        outcome.track_code = track.language_code
        outcome.track_kind = TrackKind.GENERATED if track.is_generated else TrackKind.MANUAL

    def _require_marker(self) -> None:
        """Refuse to publish once the staging marker is gone, so nothing lands on an unmounted root.

        Called after a file is staged and before it is published: raising there discards the stage.

        Raises:
            StagingUnavailable: If the marker is missing.
        """
        if not self._check_marker():
            raise StagingUnavailable(f'Staging marker {self._root / staging.MARKER_NAME} is missing')

    def _check_marker(self) -> bool:
        """Check the staging marker, record the result for ``/health`` and return it."""
        present = staging.marker_present(self._root)
        self._staging_check = StagingCheck(present, self._monotonic())
        return present

    def _marker_heartbeat(self) -> ActivityCallback:
        """Return a download callback that repeats the marker check once per ``marker_check_interval``.

        A download can outlast the age at which ``/health`` calls a check stale, while the share is
        healthy (decision C43). The result only informs ``/health``: publication checks again.
        """

        def beat() -> None:
            check = self._staging_check
            if check is None or self._monotonic() - check.checked_at >= self._marker_check_interval:
                self._check_marker()

        return beat
