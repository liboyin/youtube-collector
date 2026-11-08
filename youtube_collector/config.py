"""Service settings from the environment (DESIGN section 7).

This is the only module that reads the environment. Every other component takes explicit
parameters, so tests never depend on the process environment.
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from youtube_collector.youtube import Pacing

__all__ = ['DEFAULT_DATABASE', 'Settings', 'SettingsError']

DEFAULT_DATABASE = Path('/var/lib/youtube-collector/collector.sqlite3')


class SettingsError(ValueError):
    """Raised when an environment variable is missing or invalid; the message names it."""


@dataclass(frozen=True)
class Settings:
    """The service's settings, with DESIGN section 7's defaults.

    Attributes:
        staging_dir: The staging area inside the container (``COLLECTOR_STAGING_DIR``, required).
        database: The SQLite file on the named volume (``COLLECTOR_DATABASE``).
        pacing: yt-dlp's request pacing (``COLLECTOR_SLEEP_REQUESTS``, ``COLLECTOR_SLEEP_DOWNLOAD``,
            ``COLLECTOR_MAX_SLEEP_DOWNLOAD``); no extra sleep by default.
        job_retention: Seconds a finished job stays readable (``COLLECTOR_JOB_RETENTION_DAYS``, in days).
        host: The address the HTTP server binds (``COLLECTOR_HOST``).
        port: The HTTP port inside the container (``COLLECTOR_PORT``).
        staging_health_max_age: The age beyond which a cached staging check counts as unhealthy; an
            implementation default (decision C35).
        shutdown_grace: How long shutdown waits for the current yt-dlp call before abandoning it
            (decision C36; ``COLLECTOR_SHUTDOWN_GRACE``).
    """

    staging_dir: Path
    database: Path = DEFAULT_DATABASE
    pacing: Pacing = Pacing()
    job_retention: float = 7 * 24 * 60 * 60
    host: str = '0.0.0.0'
    port: int = 8000
    staging_health_max_age: float = 120.0
    shutdown_grace: float = 30.0

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> Self:
        """Read the settings from an environment mapping, such as ``os.environ``.

        Unset or empty variables take their defaults.

        Raises:
            SettingsError: If ``COLLECTOR_STAGING_DIR`` is missing, or a value is malformed or out of range.
        """
        staging = environ.get('COLLECTOR_STAGING_DIR', '')
        if not staging:
            raise SettingsError('COLLECTOR_STAGING_DIR must name the staging directory')
        max_sleep = _number(environ, 'COLLECTOR_MAX_SLEEP_DOWNLOAD', None)
        sleep_download = _required(_number(environ, 'COLLECTOR_SLEEP_DOWNLOAD', 0.0))
        if max_sleep is not None and max_sleep < sleep_download:
            raise SettingsError('COLLECTOR_MAX_SLEEP_DOWNLOAD must not be below COLLECTOR_SLEEP_DOWNLOAD')
        return cls(
            staging_dir=Path(staging),
            database=Path(environ.get('COLLECTOR_DATABASE') or DEFAULT_DATABASE),
            pacing=Pacing(
                sleep_requests=_required(_number(environ, 'COLLECTOR_SLEEP_REQUESTS', 0.0)),
                sleep_download=sleep_download,
                max_sleep_download=max_sleep,
            ),
            job_retention=_required(_number(environ, 'COLLECTOR_JOB_RETENTION_DAYS', 7.0)) * 86400,
            host=environ.get('COLLECTOR_HOST') or '0.0.0.0',
            port=_port(environ.get('COLLECTOR_PORT') or '8000'),
            shutdown_grace=_required(_number(environ, 'COLLECTOR_SHUTDOWN_GRACE', 30.0)),
        )


def _number(environ: Mapping[str, str], name: str, default: float | None) -> float | None:
    """Parse a finite, non-negative number, or return the default when unset.

    Raises:
        SettingsError: If the value is not such a number.
    """
    raw = environ.get(name, '')
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise SettingsError(f'{name} must be a number, got {raw!r}') from None
    if not math.isfinite(value) or value < 0:
        raise SettingsError(f'{name} must be a finite non-negative number, got {raw!r}')
    return value


def _required(value: float | None) -> float:
    """Narrow a parsed number whose default is not None."""
    assert value is not None  # every caller passes a numeric default
    return value


def _port(raw: str) -> int:
    """Parse a TCP port.

    Raises:
        SettingsError: If the value is not an integer from 1 to 65535.
    """
    try:
        port = int(raw)
    except ValueError:
        raise SettingsError(f'COLLECTOR_PORT must be an integer, got {raw!r}') from None
    if not 1 <= port <= 65535:
        raise SettingsError(f'COLLECTOR_PORT must be from 1 to 65535, got {raw!r}')
    return port
