"""Fixtures that guard every test against external connections, and own the threads and loopback servers it starts."""

import threading
from collections.abc import Callable, Iterator
from http.server import ThreadingHTTPServer

import network_guard
import pytest

JOIN_SECONDS = 10.0


class NetworkRefusals:
    """The connections and lookups the network guard refused during one test."""

    def __init__(self) -> None:
        """Start with none."""
        self._messages: list[str] = []

    def report(self, message: str) -> None:
        """Record one refusal; called from any thread the test runs."""
        self._messages.append(message)

    def take(self) -> list[str]:
        """Return the refusals so far and forget them, for a test that makes them on purpose.

        Call it in a ``finally`` when an assertion can fail before it: refusals left untaken also fail the
        test at teardown, and that second failure hides the first.
        """
        messages, self._messages[:] = self._messages[:], []
        return messages


@pytest.fixture(autouse=True)
def network_refusals() -> Iterator[NetworkRefusals]:
    """Guard each test against connections and lookups beyond loopback, then fail it if any was attempted.

    The guard raises at the attempt, but code under test may swallow that error, so the record decides.
    Its own patch context restores the socket layer after the check, also when the test failed.
    """
    refusals = NetworkRefusals()
    with pytest.MonkeyPatch.context() as patch:
        network_guard.NetworkGuard(refusals.report).install(patch.setattr)
        yield refusals
        attempts = refusals.take()
        assert not attempts, (
            f'The test tried to reach beyond loopback: {attempts}. Give the code under test a fake or a loopback '
            'endpoint; if the attempt is deliberate, take it with network_refusals.take(), in a finally if an '
            'assertion can fail before then (tests/README.md, Network).'
        )


class ThreadOwner:
    """Starts threads for one test and, on ``close``, stops them, joins them and reports survivors."""

    def __init__(self) -> None:
        """Start with nothing owned."""
        self._stops: list[Callable[[], object]] = []
        self._threads: list[tuple[threading.Thread, float]] = []

    def adopt(self, thread: threading.Thread, join_timeout: float = JOIN_SECONDS) -> None:
        """Take ownership of a thread something else started."""
        self._threads.append((thread, join_timeout))

    def start(
        self, target: Callable[[], object], *, stop: Callable[[], object] | None = None,
        join_timeout: float = JOIN_SECONDS,
    ) -> threading.Thread:
        """Start a daemon thread, so a survivor cannot hang the interpreter, and own it.

        Args:
            target: What the thread runs.
            stop: Called at ``close`` to end it, also after the test failed.
            join_timeout: How long ``close`` waits for it.

        Returns:
            The running thread.
        """
        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        self.adopt(thread, join_timeout)
        if stop is not None:
            self._stops.append(stop)
        return thread

    def close(self) -> None:
        """Run every stop, join every thread within its bound, then fail if a stop raised or a thread survives.

        A stop that raises must not abandon the rest of the cleanup, so failures are reported last.
        """
        errors = []
        for stop in self._stops:
            try:
                stop()
            except Exception as error:
                errors.append(repr(error))
        for thread, join_timeout in self._threads:
            thread.join(join_timeout)
        survivors = [thread.name for thread, _ in self._threads if thread.is_alive()]
        assert not errors, f'Stops failed during teardown: {errors}; threads still alive: {survivors}'
        assert not survivors, f'Threads still alive after teardown: {survivors}'


class LoopbackServer(ThreadingHTTPServer):
    """A loopback HTTP server that records its handler threads, so its owner can join each one within a bound.

    ``ThreadingHTTPServer`` can only join handlers without a bound, in ``server_close``, which would hang
    the test run on a handler that never finishes. Recording happens before the thread starts, so no
    handler can be missed.
    """

    def __init__(self, address, handler):
        """Bind and start with no handlers."""
        self.handler_threads: list[threading.Thread] = []
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        """Serve one request on a recorded daemon thread."""
        thread = threading.Thread(target=self.process_request_thread, args=(request, client_address), daemon=True)
        self.handler_threads.append(thread)
        thread.start()


@pytest.fixture
def owned_threads() -> Iterator[ThreadOwner]:
    """Provide a thread owner; teardown stops and joins everything and fails if anything survives."""
    owner = ThreadOwner()
    yield owner
    owner.close()


@pytest.fixture
def loopback_server(owned_threads: ThreadOwner) -> Callable[..., str]:
    """Provide a function that serves a handler on loopback and returns its URL; teardown owns every thread."""

    def start(handler: Callable[..., object], join_timeout: float = JOIN_SECONDS) -> str:
        server = LoopbackServer(('127.0.0.1', 0), handler)

        def stop() -> None:
            server.shutdown()
            server.server_close()
            for thread in server.handler_threads:
                owned_threads.adopt(thread, join_timeout)

        owned_threads.start(server.serve_forever, stop=stop)
        return f'http://127.0.0.1:{server.server_address[1]}'

    return start
