"""Tests for the service entrypoint (DESIGN section 7) and its process lifecycle (DESIGN 5.12, decision C50).

The lifecycle tests run real service processes through ``lifecycle_service.py``, with fake adapters, on
loopback and against test-owned storage. A test's SIGKILL stands in for Docker's fallback; it does not validate
Docker itself.
"""

import http.client
import json
import os
import runpy
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import lifecycle_service
import network_guard
import pytest

import youtube_collector.__main__ as testee
from youtube_collector import staging
from youtube_collector.config import SettingsError

VIDEO = 'abcdefghijk'
VIDEO_URL = f'https://youtu.be/{VIDEO}'
WAIT_SECONDS = 30.0
JSON_HEADERS = {'Content-Type': 'application/json'}
SERVICE_SCRIPT = Path(lifecycle_service.__file__)
PACKAGE_DIR = Path(testee.__file__).parent


def test_main_serves_one_process_with_the_configured_address(tmp_path):
    """The entrypoint builds the app from the environment and runs a single uvicorn server on it."""
    environ = {'COLLECTOR_STAGING_DIR': str(tmp_path), 'COLLECTOR_PORT': '9123', 'COLLECTOR_HOST': '127.0.0.1'}
    with patch.object(testee.uvicorn, 'run') as run, patch.object(testee.logging, 'basicConfig'):
        testee.main(environ)
    [app], options = run.call_args
    assert app.title == 'youtube-collector'
    assert (options['host'], options['port']) == ('127.0.0.1', 9123)
    assert options['timeout_graceful_shutdown'] == testee.REQUEST_SHUTDOWN_SECONDS
    assert 'workers' not in options  # one process: long-poll wake-ups are in-process


def test_main_fails_without_settings():
    """A missing staging directory stops the service before it serves."""
    with patch.object(testee.uvicorn, 'run') as run, patch.object(testee.logging, 'basicConfig'), \
            pytest.raises(SettingsError):
        testee.main({})
    run.assert_not_called()


def test_running_the_package_calls_main(tmp_path, monkeypatch):
    """python3 -m youtube_collector runs main with the process environment."""
    monkeypatch.setenv('COLLECTOR_STAGING_DIR', str(tmp_path))
    # A fresh execution, as `python3 -m` performs; the imported module is restored afterwards.
    monkeypatch.delitem(sys.modules, 'youtube_collector.__main__')
    with patch.object(testee.uvicorn, 'run') as run, patch.object(testee.logging, 'basicConfig'):
        runpy.run_module('youtube_collector', run_name='__main__')
    run.assert_called_once()


def wait_until(condition, description):
    """Wait for a condition another process brings about, failing the test after a bound."""
    deadline = time.monotonic() + WAIT_SECONDS
    while not condition():
        assert time.monotonic() < deadline, f'timed out waiting for {description}'
        time.sleep(0.01)


class ServiceProcess:
    """One running service process, driven over loopback and by its control directory."""

    def __init__(self, process, port, log):
        """Remember the child, its port and its combined output."""
        self.process = process
        self.port = port
        self.log = log

    def output(self):
        """Return everything the process has logged so far."""
        return self.log.read_text()

    def request(self, method, path, body=None):
        """Send one request and return its status and decoded JSON body."""
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=WAIT_SECONDS)
        try:
            connection.request(method, path, None if body is None else json.dumps(body), JSON_HEADERS)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def healthy(self):
        """Return whether the service answers /health with 200 yet."""
        assert self.process.poll() is None, f'the service exited early:\n{self.output()}'
        try:
            return self.request('GET', '/health')[0] == 200
        except OSError:
            return False

    def submit(self):
        """Submit the test's video and return the job's ID."""
        status, job = self.request('POST', '/jobs/videos', {'url': VIDEO_URL})
        assert status == 202
        return job['id']

    def send_without_waiting(self, path, body):
        """Send a request that will never be answered, returning its open connection."""
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=WAIT_SECONDS)
        connection.request('POST', path, json.dumps(body), JSON_HEADERS)
        return connection

    def job(self, job_id):
        """Return a job's current representation."""
        status, job = self.request('GET', f'/jobs/{job_id}')
        assert status == 200
        return job

    def wait_finished(self, job_id):
        """Wait for a job to finish and return it."""
        wait_until(lambda: self.job(job_id)['state'] == 'finished', 'the job to finish')
        return self.job(job_id)

    def terminate(self):
        """Send SIGTERM, as ``docker compose stop`` does."""
        self.process.send_signal(signal.SIGTERM)

    def wait_exit(self):
        """Wait for the process to exit by itself and return its exit status."""
        return self.process.wait(WAIT_SECONDS)


class Lifecycle:
    """The test-owned storage and control files shared by the service processes of one test."""

    def __init__(self, tmp_path):
        """Create the staging root with its marker, and the control directory."""
        self.tmp_path = tmp_path
        self.staging = tmp_path / 'staging'
        self.staging.mkdir()
        (self.staging / staging.MARKER_NAME).touch()
        self.control = tmp_path / 'control'
        self.control.mkdir()
        self.home = tmp_path / 'home'
        self.home.mkdir()
        self.processes = []
        self.expect_refusals = False

    def flag(self, name):
        """Return one control file."""
        return self.control / name

    def artifacts(self):
        """Return the media files at their final paths, which must only ever be complete."""
        return sorted(self.staging.glob('*.mp4'))

    def refusals(self):
        """Return the network guard's refusals that any service process of the test has logged."""
        return [
            line for log in sorted(self.tmp_path.glob('service-*.log')) for line in log.read_text().splitlines()
            if network_guard.MARKER in line
        ]

    def downloads(self):
        """Return how many downloads have started, across every process of the test."""
        downloads = self.flag('downloads')
        return len(downloads.read_text().splitlines()) if downloads.exists() else 0

    def start(self, grace='10'):
        """Start a service process on a free loopback port and wait until it is healthy."""
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        environment = {
            'PATH': os.environ['PATH'], 'HOME': str(self.home), 'TMPDIR': str(self.tmp_path),
            'PYTHONPATH': str(PACKAGE_DIR.parent), 'COLLECTOR_STAGING_DIR': str(self.staging),
            'COLLECTOR_DATABASE': str(self.tmp_path / 'collector.sqlite3'), 'COLLECTOR_HOST': '127.0.0.1',
            'COLLECTOR_PORT': str(port), 'COLLECTOR_SHUTDOWN_GRACE': grace,
        }
        log = self.tmp_path / f'service-{len(self.processes)}.log'
        with log.open('wb') as output:
            process = subprocess.Popen(
                [sys.executable, str(SERVICE_SCRIPT), str(self.control)], env=environment, cwd=self.tmp_path,
                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
            )
        self.processes.append(process)
        service = ServiceProcess(process, port, log)
        wait_until(service.healthy, 'the service to become healthy')
        # A child importing another tree would make every result below meaningless.
        assert f'PACKAGE {PACKAGE_DIR / "__init__.py"}' in service.output()
        return service

    def close(self):
        """Kill and reap every process, then fail if any survives or, unexpectedly, tried to reach beyond loopback."""
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.wait(WAIT_SECONDS)
        assert all(process.returncode is not None for process in self.processes)
        refusals = self.refusals()
        assert self.expect_refusals or not refusals, f'A service process tried to reach beyond loopback: {refusals}'


@pytest.fixture
def lifecycle(tmp_path):
    """Provide test-owned storage; teardown kills and reaps every service process, also after a failure."""
    owned = Lifecycle(tmp_path)
    try:
        yield owned
    finally:
        owned.close()


def assert_stopped_gracefully(service, exit_status):
    """Check that the application finished shutting down and uvicorn re-raised the signal it handled."""
    assert 'Application shutdown complete' in service.output()
    assert exit_status == -signal.SIGTERM


def test_sigterm_ends_an_idle_service_gracefully(lifecycle):
    """With nothing running, SIGTERM completes the application's shutdown and ends the process."""
    service = lifecycle.start()
    service.terminate()
    assert_stopped_gracefully(service, service.wait_exit())
    assert 'did not stop within the shutdown grace' not in service.output()


def test_sigterm_lets_the_current_download_finish_within_the_grace(lifecycle):
    """A download that ends within the grace is published and recorded before the process exits."""
    lifecycle.flag('hold-download').touch()
    service = lifecycle.start()
    job_id = service.submit()
    wait_until(lifecycle.flag('download-entered').exists, 'the download to start')
    service.terminate()
    wait_until(lambda: 'Shutting down' in service.output(), 'shutdown to begin')
    lifecycle.flag('hold-download').unlink()
    assert_stopped_gracefully(service, service.wait_exit())
    assert 'did not stop within the shutdown grace' not in service.output()

    restarted = lifecycle.start()
    job = restarted.job(job_id)
    assert (job['state'], job['items'][0]['media']['outcome']) == ('finished', 'downloaded')
    assert [path.read_bytes() for path in lifecycle.artifacts()] == [lifecycle_service.COMPLETE]
    assert lifecycle.downloads() == 1


def test_a_download_outlasting_the_grace_is_abandoned_and_redone_after_restart(lifecycle):
    """The process exits with the download unfinished; nothing partial is published, and restart redoes it."""
    lifecycle.flag('hold-download').touch()
    service = lifecycle.start(grace='0.2')
    job_id = service.submit()
    wait_until(lifecycle.flag('download-entered').exists, 'the download to start')
    service.terminate()
    assert_stopped_gracefully(service, service.wait_exit())
    assert 'did not stop within the shutdown grace' in service.output()
    assert lifecycle.artifacts() == []

    lifecycle.flag('hold-download').unlink()
    restarted = lifecycle.start()
    job = restarted.wait_finished(job_id)
    assert job['items'][0]['media']['outcome'] == 'downloaded'
    assert [path.read_bytes() for path in lifecycle.artifacts()] == [lifecycle_service.COMPLETE]
    assert lifecycle.downloads() == 2


def test_a_kill_between_publication_and_saving_keeps_the_published_file(lifecycle):
    """A file published before its outcome was saved is found after restart, not downloaded or overwritten."""
    lifecycle.flag('stall-save').touch()
    service = lifecycle.start()
    job_id = service.submit()
    wait_until(lifecycle.flag('save-stalled').exists, 'the outcome save to stall')
    [published] = lifecycle.artifacts()
    before = published.stat()
    service.process.kill()
    assert service.process.wait(WAIT_SECONDS) == -signal.SIGKILL

    lifecycle.flag('stall-save').unlink()
    restarted = lifecycle.start()
    job = restarted.wait_finished(job_id)
    assert job['items'][0]['media']['outcome'] == 'existing'
    after = published.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert published.read_bytes() == lifecycle_service.COMPLETE
    assert lifecycle.downloads() == 1


def test_a_stalled_api_database_keeps_the_process_alive_until_it_is_killed(lifecycle):
    """A blocked database operation outlasts graceful shutdown (C50); a kill ends it and restart recovers the work."""
    lifecycle.flag('hold-download').touch()
    service = lifecycle.start(grace='0.2')
    job_id = service.submit()
    wait_until(lifecycle.flag('download-entered').exists, 'the download to start')
    lifecycle.flag('stall-api-database').touch()
    stalled = service.send_without_waiting('/jobs/videos', {'url': VIDEO_URL})
    try:
        wait_until(lifecycle.flag('api-database-stalled').exists, 'the API database thread to stall')
        service.terminate()
        wait_until(lambda: 'did not stop within the shutdown grace' in service.output(), 'the worker to be abandoned')
        # Only time can show that exit does not happen. Without the stall, exit follows this point almost
        # at once, so a process that was going to exit does so well inside this second.
        with pytest.raises(subprocess.TimeoutExpired):
            service.process.wait(1.0)
        assert 'Application shutdown complete' not in service.output()
        service.process.kill()
        assert service.process.wait(WAIT_SECONDS) == -signal.SIGKILL
    finally:
        stalled.close()

    lifecycle.flag('stall-api-database').unlink()
    lifecycle.flag('hold-download').unlink()
    restarted = lifecycle.start()
    job = restarted.wait_finished(job_id)
    assert job['items'][0]['media']['outcome'] == 'downloaded'
    assert [path.read_bytes() for path in lifecycle.artifacts()] == [lifecycle_service.COMPLETE]
    # The request whose transaction never committed left no job behind.
    assert [entry['id'] for entry in restarted.request('GET', '/jobs')[1]['jobs']] == [job_id]


@pytest.mark.parametrize('host', ['192.0.2.1', 'example.invalid'])
def test_the_service_process_refuses_to_reach_beyond_loopback(lifecycle, host):
    """The child's guard refuses a reserved address and a reserved name, and reports them for the test to fail on.

    192.0.2.1 is TEST-NET-1 (RFC 5737) and .invalid a reserved name (RFC 2606), so a broken guard reaches nothing.
    """
    service = lifecycle.start()
    lifecycle.flag('connect-to').write_text(f'{host} 80')
    job = service.wait_finished(service.submit())
    [failure] = job['failures']
    assert network_guard.MARKER in failure['message'] and host in failure['message']
    assert [line for line in lifecycle.refusals() if host in line], service.output()
    lifecycle.expect_refusals = True


@pytest.mark.parametrize('host', ['127.0.0.1', 'localhost'])
def test_the_service_process_reaches_loopback(lifecycle, host):
    """The child's guard lets it connect to a test-owned loopback listener, by address and by name."""
    with socket.create_server(('127.0.0.1', 0)) as listener:
        service = lifecycle.start()
        lifecycle.flag('connect-to').write_text(f'{host} {listener.getsockname()[1]}')
        job = service.wait_finished(service.submit())
        assert (job['failures'], job['items'][0]['media']['outcome']) == ([], 'downloaded')
        listener.settimeout(WAIT_SECONDS)
        listener.accept()[0].close()
    assert lifecycle.refusals() == []


def test_lifecycle_teardown_fails_on_a_logged_refusal(tmp_path):
    """A refusal in any service log fails the test at teardown, even if nothing else noticed it."""
    lifecycle = Lifecycle(tmp_path)
    (tmp_path / 'service-0.log').write_text(f'starting\n{network_guard.MARKER}: connect to (\'192.0.2.1\', 80)\n')
    with pytest.raises(AssertionError, match='tried to reach beyond loopback'):
        lifecycle.close()
    lifecycle.expect_refusals = True
    lifecycle.close()
