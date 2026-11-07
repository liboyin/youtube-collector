"""Wake long-poll waiters when the database changes (DESIGN 5.10).

SQLite is the source of truth. A notification only wakes waiting requests, which then read the
database again, so a notification that is lost, early or duplicated never loses a change. The
worker and reconciliation threads call ``notify`` from their own threads; the event loop thread
owns every asyncio object.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

__all__ = ['Notifier']

logger = logging.getLogger(__name__)


class Notifier:
    """Thread-safe wake-ups for coroutines waiting on the event loop.

    Each change replaces the current wake-up event with a fresh one after setting it, so every
    waiter holding the old event wakes once. A waiter takes its ticket before reading the database:
    a change committed after the read but before the wait has then already set that ticket.

    Args:
        monotonic: The clock ``long_poll`` measures its timeout on; injected for tests.
    """

    def __init__(self, monotonic: Callable[[], float] = time.monotonic) -> None:
        """Create an unbound notifier; ``bind`` attaches it to the event loop."""
        self._monotonic = monotonic
        self._loop: asyncio.AbstractEventLoop | None = None
        self._event: asyncio.Event | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        """Attach the event loop that waiters run on; call from that loop's thread."""
        self._loop = loop
        self._event = asyncio.Event()

    def notify(self) -> None:
        """Wake every current waiter; safe from any thread, and a no-op when no loop is running."""
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._wake)
        except RuntimeError:
            # The loop has closed, as during shutdown; nobody is left waiting.
            logger.debug('Notification after the event loop closed was dropped')

    def _wake(self) -> None:
        """Set the current event and start a fresh one; runs on the event loop thread.

        Only scheduled on a bound loop, so the current event exists.
        """
        self.ticket().set()
        self._event = asyncio.Event()

    def ticket(self) -> asyncio.Event:
        """Return the event the next notification will set; take it before reading the database.

        Raises:
            RuntimeError: If the notifier is not bound to a loop.
        """
        if self._event is None:
            raise RuntimeError('Notifier is not bound to an event loop')
        return self._event

    async def wait(self, ticket: asyncio.Event, timeout: float) -> bool:
        """Wait up to ``timeout`` seconds for the notification a ticket names.

        Returns:
            True when notified, False on timeout.
        """
        try:
            await asyncio.wait_for(ticket.wait(), timeout)
        except TimeoutError:
            return False
        return True

    async def long_poll[T](
        self, read: Callable[[], Awaitable[T]], ready: Callable[[T], bool], timeout: float,
    ) -> T:
        """Read until ``ready`` holds or ``timeout`` seconds pass, waking on notifications.

        Returns at once when the first read is ready; otherwise waits for a change, reads again, and
        so on, returning the last read when the time is up.

        Args:
            read: Reads the database; called at least once.
            ready: Whether a read has something to return now.
            timeout: The longest wait in seconds; 0 reads once.
        """
        deadline = self._monotonic() + timeout
        while True:
            ticket = self.ticket()
            result = await read()
            remaining = deadline - self._monotonic()
            if ready(result) or remaining <= 0:
                return result
            await self.wait(ticket, remaining)
