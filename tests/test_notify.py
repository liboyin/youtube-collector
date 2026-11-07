"""Tests for long-poll wake-ups (DESIGN 5.10)."""

import asyncio
import threading
import time

import pytest

import youtube_collector.notify as testee


class Clock:
    """A manually advanced monotonic clock."""

    def __init__(self):
        """Start at zero."""
        self.now = 0.0

    def __call__(self):
        """Return the current time."""
        return self.now


def run(coroutine_function, notifier):
    """Run a coroutine on a fresh event loop bound to the notifier, and close the loop afterwards."""
    async def main():
        notifier.bind(asyncio.get_running_loop())
        return await coroutine_function()

    return asyncio.run(main())


def test_notify_before_binding_is_a_no_op():
    """Threads may notify before the API's loop exists; nothing is waiting, so nothing is lost."""
    testee.Notifier().notify()


def test_ticket_requires_a_bound_loop():
    """Waiting needs the event loop."""
    with pytest.raises(RuntimeError):
        testee.Notifier().ticket()


def test_notify_from_another_thread_wakes_a_waiter():
    """A notification from a worker thread wakes a coroutine waiting on the loop."""
    notifier = testee.Notifier()

    waiting = threading.Event()

    def notify_once_waiting():
        waiting.wait(10)
        time.sleep(0.2)  # a bounded pause, so the loop is asleep in its selector when the notification comes
        notifier.notify()

    async def scenario():
        ticket = notifier.ticket()
        thread = threading.Thread(target=notify_once_waiting)
        thread.start()
        loop = asyncio.get_running_loop()
        try:
            waiting.set()
            started = loop.time()
            woken = await notifier.wait(ticket, 10)
            return woken, loop.time() - started
        finally:
            waiting.set()
            thread.join(10)

    woken, latency = run(scenario, notifier)
    assert woken
    # A thread-safe wake-up arrives just after the 0.2 s pause; one that is not thread-safe is only
    # noticed when the loop next wakes for another reason, here the 10 s timeout.
    assert latency < 5


def test_a_notification_between_ticket_and_wait_is_not_lost():
    """A change committed after the ticket was taken, but before waiting, still wakes the waiter."""
    notifier = testee.Notifier()

    async def scenario():
        ticket = notifier.ticket()
        notifier.notify()
        await asyncio.sleep(0)  # let the loop run the thread-safe callback before waiting
        return await notifier.wait(ticket, 10)

    assert run(scenario, notifier) is True


def test_each_notification_starts_a_fresh_ticket():
    """After a wake-up, a new ticket waits for the next notification rather than the old one."""
    notifier = testee.Notifier()

    async def scenario():
        old = notifier.ticket()
        notifier.notify()
        await notifier.wait(old, 10)
        return old.is_set(), notifier.ticket().is_set()

    assert run(scenario, notifier) == (True, False)


def test_wait_times_out_without_a_notification():
    """No notification means the wait ends at its timeout."""
    notifier = testee.Notifier()

    async def scenario():
        return await notifier.wait(notifier.ticket(), 0.01)

    assert run(scenario, notifier) is False


def test_notify_after_the_loop_closed_is_dropped():
    """A thread that notifies during shutdown, after the loop closed, does not crash."""
    notifier = testee.Notifier()

    async def scenario():
        return None

    run(scenario, notifier)
    notifier.notify()


def test_long_poll_returns_at_once_when_ready():
    """A ready first read returns without waiting."""
    notifier = testee.Notifier()
    reads = []

    async def read():
        reads.append(1)
        return 'changes'

    async def scenario():
        return await notifier.long_poll(read, lambda result: True, 60)

    assert run(scenario, notifier) == 'changes'
    assert reads == [1]


def test_long_poll_reads_again_after_a_notification():
    """A waiter rereads the database after each wake-up and returns once something is ready."""
    notifier = testee.Notifier()
    results = iter([None, 'changes'])
    reads = []
    threads = []

    async def read():
        reads.append(1)
        result = next(results)
        if result is None:
            threads.append(threading.Thread(target=notifier.notify))  # a change commits after this read
            threads[-1].start()
        return result

    async def scenario():
        try:
            return await notifier.long_poll(read, lambda result: result is not None, 60)
        finally:
            for thread in threads:
                thread.join(10)

    assert run(scenario, notifier) == 'changes'
    assert len(reads) == 2
    assert not any(thread.is_alive() for thread in threads)


def test_long_poll_takes_its_ticket_before_reading():
    """A change notified while the read is in progress wakes the waiter, so the change is not missed."""
    notifier = testee.Notifier()
    results = iter([None, 'changes'])

    async def read():
        result = next(results)
        if result is None:
            notifier.notify()  # a change commits and notifies while this read is still in progress
            await asyncio.sleep(0)  # the wake-up runs before the read returns
        return result

    async def scenario():
        return await asyncio.wait_for(notifier.long_poll(read, lambda result: result is not None, 60), 5)

    assert run(scenario, notifier) == 'changes'


def test_long_poll_returns_the_last_read_after_the_timeout():
    """With no change before the deadline, the last read is returned."""
    clock = Clock()
    notifier = testee.Notifier(monotonic=clock)
    reads = []

    async def read():
        reads.append(1)
        clock.now += 1  # each read costs a simulated second
        return None

    async def scenario():
        return await notifier.long_poll(read, lambda result: result is not None, 1)

    assert run(scenario, notifier) is None
    assert reads == [1]


def test_long_poll_with_zero_timeout_reads_once():
    """A zero wait reads once and returns."""
    notifier = testee.Notifier(monotonic=Clock())
    reads = []

    async def read():
        reads.append(1)
        return None

    async def scenario():
        return await notifier.long_poll(read, lambda result: False, 0)

    assert run(scenario, notifier) is None
    assert reads == [1]
