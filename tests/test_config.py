"""Tests for settings from the environment (DESIGN section 7)."""

from pathlib import Path

import pytest

import youtube_collector.config as testee
from youtube_collector.youtube import Pacing


def test_from_environ_applies_the_design_defaults():
    """Only the staging directory is required; everything else has DESIGN section 7's defaults."""
    settings = testee.Settings.from_environ({'COLLECTOR_STAGING_DIR': '/staging'})
    assert settings == testee.Settings(staging_dir=Path('/staging'))
    assert (settings.database, settings.pacing) == (
        Path('/var/lib/youtube-collector/collector.sqlite3'), Pacing(0.0, 0.0, None),
    )
    assert (settings.job_retention, settings.host, settings.port) == (7 * 86400, '0.0.0.0', 8000)


def test_from_environ_reads_every_setting():
    """Each variable sets its setting; retention is given in days."""
    settings = testee.Settings.from_environ({
        'COLLECTOR_STAGING_DIR': '/s', 'COLLECTOR_DATABASE': '/db/c.sqlite3', 'COLLECTOR_SLEEP_REQUESTS': '1.5',
        'COLLECTOR_SLEEP_DOWNLOAD': '2', 'COLLECTOR_MAX_SLEEP_DOWNLOAD': '4', 'COLLECTOR_JOB_RETENTION_DAYS': '0.5',
        'COLLECTOR_HOST': '127.0.0.1', 'COLLECTOR_PORT': '9000', 'COLLECTOR_SHUTDOWN_GRACE': '5',
    })
    assert settings == testee.Settings(
        staging_dir=Path('/s'), database=Path('/db/c.sqlite3'), pacing=Pacing(1.5, 2.0, 4.0), job_retention=43200.0,
        host='127.0.0.1', port=9000, shutdown_grace=5.0,
    )


def test_from_environ_treats_empty_values_as_unset():
    """An empty variable, as Compose passes for an unset one, takes the default."""
    environ = {'COLLECTOR_STAGING_DIR': '/s', 'COLLECTOR_PORT': '', 'COLLECTOR_JOB_RETENTION_DAYS': ''}
    settings = testee.Settings.from_environ(environ)
    assert (settings.port, settings.job_retention) == (8000, 7 * 86400)


@pytest.mark.parametrize('environ', [{}, {'COLLECTOR_STAGING_DIR': ''}])
def test_from_environ_requires_the_staging_directory(environ):
    """Without a staging directory the service cannot start."""
    with pytest.raises(testee.SettingsError, match='COLLECTOR_STAGING_DIR'):
        testee.Settings.from_environ(environ)


@pytest.mark.parametrize(('name', 'value'), [
    ('COLLECTOR_SLEEP_REQUESTS', 'fast'), ('COLLECTOR_SLEEP_REQUESTS', '-1'), ('COLLECTOR_JOB_RETENTION_DAYS', 'nan'),
    ('COLLECTOR_JOB_RETENTION_DAYS', 'inf'), ('COLLECTOR_PORT', '0'),
    ('COLLECTOR_PORT', '65536'), ('COLLECTOR_PORT', 'http'), ('COLLECTOR_SHUTDOWN_GRACE', '-5'),
])
def test_from_environ_rejects_invalid_values(name, value):
    """Malformed, negative, non-finite or out-of-range values fail at startup, naming the variable."""
    with pytest.raises(testee.SettingsError, match=name):
        testee.Settings.from_environ({'COLLECTOR_STAGING_DIR': '/s', name: value})


def test_from_environ_rejects_a_sleep_range_that_is_upside_down():
    """The maximum download sleep may not be below the minimum."""
    with pytest.raises(testee.SettingsError, match='MAX_SLEEP'):
        testee.Settings.from_environ({
            'COLLECTOR_STAGING_DIR': '/s', 'COLLECTOR_SLEEP_DOWNLOAD': '5', 'COLLECTOR_MAX_SLEEP_DOWNLOAD': '1',
        })


def test_from_environ_ignores_the_removed_variables():
    """Removed settings have no effect and stop being read, so a stale .env is harmless (C44)."""
    settings = testee.Settings.from_environ({
        'COLLECTOR_STAGING_DIR': '/s', 'COLLECTOR_RECONCILE_INTERVAL': 'x', 'COLLECTOR_MAX_WAIT': 'x',
        'COLLECTOR_TOMBSTONE_RETENTION_DAYS': 'x',
    })
    assert settings == testee.Settings(staging_dir=Path('/s'))
