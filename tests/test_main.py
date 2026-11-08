"""Tests for the service entrypoint (DESIGN section 7); no server is started."""

import runpy
import sys
from unittest.mock import patch

import pytest

import youtube_collector.__main__ as testee
from youtube_collector.config import SettingsError


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
