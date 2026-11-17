"""A real service process with fake adapters, which the lifecycle tests in ``test_main.py`` start and stop.

Run as ``python3 lifecycle_service.py CONTROL_DIR`` with the service's usual environment. It goes through the
real entrypoint, replacing only the adapters and clock, so it cannot reach YouTube, and it installs the network
guard for its whole life, reporting each refused attempt in its output. Files in the control directory are how
a test steers it from another process:

- While ``hold-download`` exists, a download waits after writing partial bytes (``download-entered`` announces it).
- With ``stall-api-database``, the API's database thread blocks for good inside its next transaction.
- With ``stall-save``, the worker blocks for good while saving the outcome of a finished download.
- With ``fail-save``, saving the outcome of a finished download fails once; the flag is then removed.
- With ``fail-expansion-save``, saving an expanded playlist fails once; the flag is then removed.
- With ``connect-to`` holding ``HOST PORT``, each metadata request first opens a connection there and closes it.

Each stall announces itself through a ``*-stalled`` file. Each download appends a line to ``downloads``, and each
playlist expansion to ``expansions``. The fatal pause is shortened, so a failed worker ends the process quickly.
"""

import dataclasses
import socket
import sqlite3
import sys
import threading
import time
from pathlib import Path

import network_guard

import youtube_collector
import youtube_collector.__main__ as entrypoint
from youtube_collector.api import Components
from youtube_collector.youtube import Playlist, PlaylistVideo, VideoInfo

PARTIAL = b'partial'
COMPLETE = b'complete media'
# Shortened so a stalled request does not cost each test uvicorn's full 5 seconds.
REQUEST_SHUTDOWN_SECONDS = 0.2
POLL_SECONDS = 0.01
CONNECT_SECONDS = 10.0
FATAL_PAUSE_SECONDS = 0.2
PLAYLIST_VIDEO = 'abcdefghijk'


class FakeMedia:
    """A media adapter whose downloads the control directory can hold."""

    def __init__(self, control):
        """Remember the control directory."""
        self.control = control

    def expand_playlist(self, url):
        """Return a one-entry playlist, recording the expansion."""
        with (self.control / 'expansions').open('a') as expansions:
            expansions.write(f'{url}\n')
        (self.control / 'expanded').touch()
        return Playlist('A list', (PlaylistVideo(PLAYLIST_VIDEO, 'A video'),))

    def video_info(self, url):
        """Return metadata for the video in the URL, first connecting where ``connect-to`` says, if it exists."""
        target = self.control / 'connect-to'
        if target.exists():
            host, port = target.read_text().split()
            socket.create_connection((host, int(port)), timeout=CONNECT_SECONDS).close()
        video_id = url.rsplit('=', 1)[1]
        return VideoInfo(video_id, 'A video', {'id': video_id})

    def download_media(self, video, path, on_activity):
        """Write partial bytes, wait while held, then complete the file."""
        with (self.control / 'downloads').open('a') as downloads:
            downloads.write(f'{video.video_id}\n')
        path.write_bytes(PARTIAL)
        (self.control / 'download-entered').touch()
        while (self.control / 'hold-download').exists():
            on_activity()
            time.sleep(POLL_SECONDS)
        path.write_bytes(COMPLETE)
        (self.control / 'download-finished').touch()


class FakeCaptions:
    """A caption adapter with no tracks; these tests submit media jobs only."""

    def find_track(self, video_id, languages):
        """Report that no track matches."""
        return None

    def fetch_srt(self, track):
        """Refuse, because no track is ever found."""
        raise NotImplementedError(track)


def controlled_clock(control):
    """Return the store's clock, which blocks the calling thread for good when a stall is requested.

    Every store transaction reads this clock, so blocking here holds a real transaction open at a chosen point.
    """

    def stall(name):
        (control / f'{name}-stalled').touch()
        threading.Event().wait()

    def fail_once(name):
        (control / name).unlink()
        raise sqlite3.OperationalError(f'injected failure: {name}')

    def clock():
        thread = threading.current_thread().name
        if thread.startswith('api-db') and (control / 'stall-api-database').exists():
            stall('api-database')
        # After a finished download the worker's next clock read is the write that saves its outcome, and
        # after an expansion it is the write that saves the entries.
        if thread == 'youtube-worker' and (control / 'download-finished').exists():
            if (control / 'stall-save').exists():
                stall('save')
            if (control / 'fail-save').exists():
                fail_once('fail-save')
        if thread == 'youtube-worker' and (control / 'expanded').exists() \
                and (control / 'fail-expansion-save').exists():
            fail_once('fail-expansion-save')
        return time.time()

    return clock


def main():
    """Guard the process, then run the real entrypoint with fake adapters, printing where the package came from."""
    network_guard.guard_process()
    control = Path(sys.argv[1])
    print(f'PACKAGE {youtube_collector.__file__}', flush=True)
    real_create_app = entrypoint.create_app
    components = Components(media=FakeMedia(control), captions=FakeCaptions(), clock=controlled_clock(control))
    entrypoint.create_app = lambda settings: real_create_app(
        dataclasses.replace(settings, fatal_pause=FATAL_PAUSE_SECONDS), lambda _settings: components,
    )
    entrypoint.REQUEST_SHUTDOWN_SECONDS = REQUEST_SHUTDOWN_SECONDS
    entrypoint.main()


if __name__ == '__main__':
    main()
