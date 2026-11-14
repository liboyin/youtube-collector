"""Check that the shared fixtures in tests/conftest.py fail a test that reaches beyond loopback or leaks threads."""

import importlib.util
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

CONFTEST = Path(__file__).resolve().parents[1] / 'conftest.py'
NETWORK_GUARD = CONFTEST.with_name('network_guard.py')


def load_conftest():
    """Load the shared conftest by file path, for a direct check of its helper class."""
    spec = importlib.util.spec_from_file_location('shared_conftest', CONFTEST)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_nested(tmp_path, source):
    """Run one test file with copies of the shared conftest and the guard it imports, in an owned process.

    Returns:
        The process's exit status and its combined output.
    """
    shutil.copy2(CONFTEST, tmp_path / 'conftest.py')
    shutil.copy2(NETWORK_GUARD, tmp_path / 'network_guard.py')
    (tmp_path / 'test_nested.py').write_text(source, encoding='utf-8')
    done = subprocess.run(
        [sys.executable, '-m', 'pytest', 'test_nested.py', '--no-cov', '-q', '-p', 'no:cacheprovider',
         '-p', 'no:randomly', f'--basetemp={tmp_path / "base"}', '--rootdir', str(tmp_path)],
        cwd=tmp_path, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60,
        env={'PATH': os.environ['PATH'], 'HOME': str(tmp_path), 'TMPDIR': str(tmp_path), 'LC_ALL': 'C.UTF-8'},
    )
    return done.returncode, done.stdout + done.stderr


def test_a_swallowed_external_connection_fails_teardown(tmp_path):
    """Code that catches the guard's error still fails its test, at teardown, naming the destination and the remedy."""
    status, output = run_nested(tmp_path, '''
import socket


def test_swallow():
    try:
        socket.create_connection(('192.0.2.1', 9), timeout=1)
    except OSError:
        pass
''')
    assert status == 1, output
    assert 'ERROR at teardown of test_swallow' in output
    assert 'The test tried to reach beyond loopback' in output and '192.0.2.1' in output
    assert 'network_refusals.take(), in a finally' in output


def test_taken_refusals_and_loopback_pass_teardown(tmp_path):
    """A test that takes its deliberate refusals passes, and loopback leaves nothing to take."""
    status, output = run_nested(tmp_path, '''
import socket


def test_deliberate(network_refusals):
    try:
        socket.getaddrinfo('example.invalid', 80)
    except OSError:
        pass
    assert len(network_refusals.take()) == 1
    socket.getaddrinfo('localhost', 80)
''')
    assert status == 0, output


def test_the_socket_layer_is_restored_after_each_test(tmp_path):
    """The guard is in place during a test and gone after its teardown."""
    status, output = run_nested(tmp_path, '''
import socket

UNGUARDED = socket.getaddrinfo


def test_guarded():
    assert socket.getaddrinfo is not UNGUARDED
    assert 'connect' in vars(socket.socket)


# Runs after the last test's function fixtures, the guard's included, have been torn down.
def teardown_module():
    assert socket.getaddrinfo is UNGUARDED
    assert not {'connect', 'connect_ex', 'sendto', 'sendmsg'} & set(vars(socket.socket))
''')
    assert status == 0, output
    assert '1 passed' in output


def test_a_leaked_thread_fails_teardown(tmp_path):
    """A thread that outlives its bound is an error at teardown."""
    status, output = run_nested(tmp_path, '''
import threading


def test_leak(owned_threads):
    owned_threads.start(threading.Event().wait, join_timeout=0.05)
''')
    assert status == 1, output
    assert 'ERROR at teardown of test_leak' in output
    assert 'Threads still alive after teardown' in output


def test_a_finished_thread_passes_teardown(tmp_path):
    """A thread that ends within its bound leaves the test passing."""
    status, output = run_nested(tmp_path, '''
def test_clean(owned_threads):
    owned_threads.start(lambda: None)
''')
    assert status == 0, output


def test_stop_runs_even_when_the_test_failed(tmp_path):
    """Teardown stops threads after a failure too, so the failure stays the only report."""
    status, output = run_nested(tmp_path, '''
import threading


def test_fails(owned_threads):
    stop = threading.Event()
    owned_threads.start(stop.wait, stop=stop.set)
    raise AssertionError('the test failed')
''')
    assert status == 1, output
    assert '1 failed' in output
    assert 'ERROR at teardown' not in output


def test_every_stop_runs_before_any_join():
    """Joining one thread must never wait on another's stop, so all stops come first."""
    owner = load_conftest().ThreadOwner()
    order = []
    events = threading.Event(), threading.Event()
    threads = []
    for event in events:
        def stop(stopped=event):
            order.append('stop')
            stopped.set()

        thread = owner.start(event.wait, stop=stop)
        threads.append(thread)
        real_join = thread.join
        thread.join = lambda timeout, real=real_join: (order.append('join'), real(timeout))
    try:
        owner.close()
    finally:  # independent of the code under test, which may be what failed
        for event in events:
            event.set()
        for thread in threads:
            threading.Thread.join(thread, 10)
    assert order == ['stop', 'stop', 'join', 'join']
    assert not any(thread.is_alive() for thread in threads)


def test_a_raising_stop_does_not_abandon_the_rest_of_teardown(tmp_path):
    """A stop that raises is reported, after every other stop has run and every thread was joined."""
    status, output = run_nested(tmp_path, '''
import threading

import pytest

first, second = threading.Event(), threading.Event()
threads = []


# Requested first, so it runs after owned_threads' teardown and always releases what a mutant leaked.
@pytest.fixture
def verify():
    yield
    try:
        assert second.is_set()
    finally:
        first.set()
        second.set()
        for thread in threads:
            thread.join(10)


def test_raising_stop(verify, owned_threads):
    def broken():
        first.set()
        raise RuntimeError('stop failed')

    threads.append(owned_threads.start(first.wait, stop=broken))
    threads.append(owned_threads.start(second.wait, stop=second.set))
''')
    assert status == 1, output
    assert 'ERROR at teardown of test_raising_stop' in output
    assert 'stop failed' in output
    assert '1 passed, 1 error' in output
    assert 'still alive: []' in output


def test_a_leaked_request_handler_fails_teardown(tmp_path):
    """A handler still running after shutdown is an error, within a bound rather than a hang."""
    status, output = run_nested(tmp_path, '''
import socket
import threading
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlsplit

entered = threading.Event()


class Handler(BaseHTTPRequestHandler):
    def handle(self):
        entered.set()
        threading.Event().wait()


def test_leak(loopback_server):
    url = urlsplit(loopback_server(Handler, join_timeout=0.05))
    client = socket.create_connection((url.hostname, url.port), timeout=10)
    try:
        assert entered.wait(10)
    finally:
        client.close()
''')
    assert status == 1, output
    assert 'ERROR at teardown of test_leak' in output
    assert 'Threads still alive after teardown' in output


def test_a_running_request_handler_is_joined(tmp_path):
    """A handler still running at teardown is waited for: it finishes only once its join has begun."""
    status, output = run_nested(tmp_path, '''
import socket
import threading
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlsplit

import pytest

entered, release = threading.Event(), threading.Event()
handlers, finished = [], []
real_join = threading.Thread.join


class Handler(BaseHTTPRequestHandler):
    def handle(self):
        handlers.append(threading.current_thread())
        entered.set()
        release.wait(30)
        finished.append(1)


def join(self, timeout=None):
    if self in handlers:
        release.set()
    return real_join(self, timeout)


# Requested first, so it runs after the fixture's teardown and always releases what a mutant leaked.
@pytest.fixture
def verify():
    yield
    try:
        assert finished == [1]
    finally:
        release.set()
        for thread in handlers:
            real_join(thread, 10)


# monkeypatch comes before loopback_server so that it is torn down after the fixture's joins.
def test_running(verify, monkeypatch, loopback_server):
    monkeypatch.setattr(threading.Thread, 'join', join)
    url = urlsplit(loopback_server(Handler))
    client = socket.create_connection((url.hostname, url.port), timeout=10)
    try:
        assert entered.wait(10)
    finally:
        client.close()
''')
    assert status == 0, output


def test_loopback_server_serves_and_stops(tmp_path):
    """The server answers a request, and teardown leaves nothing behind."""
    (tmp_path / 'served').mkdir()
    (tmp_path / 'served/a.txt').write_text('hello', encoding='utf-8')
    status, output = run_nested(tmp_path, f'''
import functools
import urllib.request
from http.server import SimpleHTTPRequestHandler


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


def test_get(loopback_server):
    url = loopback_server(functools.partial(Quiet, directory={str(tmp_path / "served")!r}))
    with urllib.request.urlopen(url + '/a.txt', timeout=10) as response:
        assert response.read() == b'hello'
''')
    assert status == 0, output
