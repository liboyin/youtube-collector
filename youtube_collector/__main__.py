"""Run the service: ``python3 -m youtube_collector`` (DESIGN section 7).

One uvicorn process serves the API and runs the worker thread; it must never
run several server processes, because the worker and its cached staging check are in-process. Settings come from the
environment (``config.Settings``). On SIGTERM uvicorn stops serving, and the application's lifespan
stops the thread within the shutdown grace (decision C36).
"""

import logging
import os
from collections.abc import Mapping

import uvicorn

from youtube_collector.api import create_app
from youtube_collector.config import Settings

__all__ = ['main']

# How long uvicorn lets open requests finish after SIGTERM before the
# application's own shutdown begins.
REQUEST_SHUTDOWN_SECONDS = 5


def main(environ: Mapping[str, str] = os.environ) -> None:
    """Read the settings, configure logging, and serve until stopped.

    Raises:
        config.SettingsError: If a setting is missing or invalid.
    """
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    settings = Settings.from_environ(environ)
    uvicorn.run(
        create_app(settings), host=settings.host, port=settings.port,
        timeout_graceful_shutdown=REQUEST_SHUTDOWN_SECONDS, log_config=None,
    )


if __name__ == '__main__':
    main()
