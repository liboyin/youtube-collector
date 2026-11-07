"""The yt-dlp adapter: playlist expansion, video metadata and MP4 downloads (DESIGN 5.3, 5.5, 5.11).

The adapter reports facts and raises yt-dlp's errors unchanged; the worker decides outcomes and
categorizes failures. Only the worker thread may use it, because all YouTube traffic runs there
one request at a time (DESIGN 5.3).
"""

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Protocol, Self

import yt_dlp

__all__ = [
    'MEDIA_FORMAT', 'Downloader', 'Pacing', 'Playlist', 'PlaylistVideo', 'ProgressCallback', 'VideoInfo',
    'YoutubeClient', 'firefox_cookies_present', 'firefox_profile_roots',
]

logger = logging.getLogger(__name__)

# The source revision's format selection: MP4 video with M4A audio, or a single MP4 file (DESIGN 5.5).
MEDIA_FORMAT = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]'

type ProgressCallback = Callable[[int, int | None], None]
type Options = dict[str, object]


class Downloader(Protocol):
    """The part of ``yt_dlp.YoutubeDL`` the adapter uses, so tests can substitute a fake."""

    def __enter__(self) -> Self:
        """Open the downloader."""
        ...

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None,
    ) -> object:
        """Close the downloader and its cookie jar."""
        ...

    def extract_info(self, url: str, download: bool = ..., process: bool = ...) -> Mapping[str, object] | None:
        """Extract information about a URL."""
        ...

    def process_ie_result(self, ie_result: Mapping[str, object], download: bool = ...) -> object:
        """Process extracted information, downloading when asked."""
        ...


@dataclass(frozen=True)
class Pacing:
    """yt-dlp's request pacing, from configuration rather than requests (DESIGN 5.3).

    Attributes:
        sleep_requests: Seconds to sleep between requests during extraction.
        sleep_download: Seconds to sleep before each download, or the lower bound of a random range.
        max_sleep_download: The upper bound of that range, or None for a fixed sleep.
    """

    sleep_requests: float = 0.0
    sleep_download: float = 0.0
    max_sleep_download: float | None = None


@dataclass(frozen=True)
class PlaylistVideo:
    """One entry of an expanded playlist.

    Attributes:
        video_id: The entry's video ID.
        title: Its title when the listing supplied one.
    """

    video_id: str
    title: str | None


@dataclass(frozen=True)
class Playlist:
    """An expanded playlist.

    Attributes:
        title: The playlist's title.
        entries: Its videos in playlist order, without deleted or private entries yt-dlp left empty.
    """

    title: str
    entries: tuple[PlaylistVideo, ...]


@dataclass(frozen=True)
class VideoInfo:
    """A video's metadata, extracted once and reused for its download.

    Attributes:
        video_id: The video ID yt-dlp reported.
        title: The video's title.
        info: yt-dlp's unprocessed extraction result, which ``download_media`` processes.
    """

    video_id: str
    title: str
    info: Mapping[str, object] = field(repr=False)


def firefox_profile_roots(home: Path, config_home: Path | None = None) -> tuple[Path, ...]:
    """Return the standard Linux Firefox profile roots that yt-dlp searches for cookies.

    Args:
        home: The home directory.
        config_home: ``XDG_CONFIG_HOME``, defaulting to ``home/.config``.
    """
    config = home / '.config' if config_home is None else config_home
    return (
        config / 'mozilla/firefox',
        home / '.mozilla/firefox',
        home / '.var/app/org.mozilla.firefox/config/mozilla/firefox',
        home / '.var/app/org.mozilla.firefox/.mozilla/firefox',
        home / 'snap/firefox/common/.mozilla/firefox',
    )


def firefox_cookies_present(roots: Sequence[Path]) -> bool:
    """Return whether a Firefox ``cookies.sqlite`` exists in a profile under any root (DESIGN 5.11).

    Only names are checked; no file is opened, because the profile holds live session cookies.
    The patterns are yt-dlp's: the root itself, a profile directory, or ``Profiles/<profile>``.
    """
    for root in roots:
        for pattern in ('cookies.sqlite', '*/cookies.sqlite', 'Profiles/*/cookies.sqlite'):
            if any(root.glob(pattern)):
                return True
    return False


class _YtDlpLogger:
    """Route yt-dlp's messages to logging instead of the console."""

    def debug(self, message: str) -> None:
        """Log yt-dlp's debug and info output at debug level."""
        logger.debug(message)

    def info(self, message: str) -> None:
        """Log yt-dlp's info output at debug level."""
        logger.debug(message)

    def warning(self, message: str) -> None:
        """Log a yt-dlp warning."""
        logger.warning(message)

    def error(self, message: str) -> None:
        """Log a yt-dlp error, which yt-dlp also raises."""
        logger.error(message)


class _ProgressHook:
    """Turn yt-dlp's per-file progress into cumulative bytes for one media download.

    A video-plus-audio download fetches two files. Downloaded bytes accumulate across them; the
    total is the known size of the files so far, and None while the current file's size is unknown.
    """

    def __init__(self, on_progress: ProgressCallback) -> None:
        """Remember where to report progress."""
        self._on_progress = on_progress
        self._completed = 0

    def __call__(self, status: Mapping[str, object]) -> None:
        """Handle one yt-dlp progress report."""
        downloaded = _int_or_none(status.get('downloaded_bytes')) or 0
        total = _int_or_none(status.get('total_bytes'))
        if status.get('status') == 'downloading':
            self._on_progress(self._completed + downloaded, None if total is None else self._completed + total)
        elif status.get('status') == 'finished':
            self._completed += total if total is not None else downloaded


def _int_or_none(value: object) -> int | None:
    """Return an integer byte count, or None for anything else."""
    return int(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


class YoutubeClient:
    """yt-dlp with the collector's options, used only from the worker thread.

    Args:
        cookies: Whether to use Firefox cookies, as detected once per process.
        pacing: Request pacing from configuration.
        factory: Builds a downloader from options; ``yt_dlp.YoutubeDL`` outside tests.
    """

    def __init__(
        self, *, cookies: bool, pacing: Pacing, factory: Callable[[Options], Downloader] = yt_dlp.YoutubeDL,
    ) -> None:
        """Store the configuration; nothing contacts YouTube until a method is called."""
        self._cookies = cookies
        self._pacing = pacing
        self._factory = factory

    def _options(self, **extra: object) -> Options:
        """Return the options every request shares, plus ``extra``."""
        options: Options = {
            'quiet': True,
            'noprogress': True,
            'logger': _YtDlpLogger(),
            # The default runtime, stated so a yt-dlp default change cannot drop it (DESIGN 5.5).
            'js_runtimes': {'deno': {}},
            'sleep_interval_requests': self._pacing.sleep_requests,
            'sleep_interval': self._pacing.sleep_download,
        }
        if self._pacing.max_sleep_download is not None:
            options['max_sleep_interval'] = self._pacing.max_sleep_download
        if self._cookies:
            options['cookiesfrombrowser'] = ('firefox',)
        options.update(extra)
        return options

    def expand_playlist(self, url: str) -> Playlist:
        """List a playlist's entries without fetching each video (DESIGN 5.3).

        Entries that yt-dlp returns as empty or without an ID, typically deleted or private videos,
        are skipped, as at the source revision.

        Raises:
            yt_dlp.utils.DownloadError: If the playlist cannot be listed.
            ValueError: If yt-dlp returns no playlist.
        """
        with self._factory(self._options(extract_flat='in_playlist')) as downloader:
            info = downloader.extract_info(url, download=False)
        if info is None:
            raise ValueError(f'yt-dlp returned nothing for {url}')
        entries = info.get('entries')
        listed = entries if isinstance(entries, Sequence) else ()
        return Playlist(
            title=_text(info.get('title')) or _text(info.get('id')) or url,
            entries=tuple(
                PlaylistVideo(_text(entry.get('id')) or '', _text(entry.get('title')))
                for entry in listed if isinstance(entry, Mapping) and _text(entry.get('id'))
            ),
        )

    def video_info(self, url: str) -> VideoInfo:
        """Extract a video's metadata, without selecting formats or downloading.

        The result is kept for ``download_media``, so a download costs no second extraction.

        Raises:
            yt_dlp.utils.DownloadError: If the video cannot be extracted.
            ValueError: If yt-dlp returns no video or no ID.
        """
        with self._factory(self._options(noplaylist=True)) as downloader:
            info = downloader.extract_info(url, download=False, process=False)
        video_id = None if info is None else _text(info.get('id'))
        if info is None or not video_id:
            raise ValueError(f'yt-dlp returned no video for {url}')
        return VideoInfo(video_id=video_id, title=_text(info.get('title')) or '', info=info)

    def download_media(self, video: VideoInfo, path: Path, on_progress: ProgressCallback) -> None:
        """Download a video as MP4 to ``path``, which normally lies in a fresh stage directory.

        yt-dlp may create temporary files beside ``path``; it merges video and audio into ``path``.

        Raises:
            yt_dlp.utils.DownloadError: If the download fails.
        """
        options = self._options(
            noplaylist=True,
            format=MEDIA_FORMAT,
            merge_output_format='mp4',
            # A literal path: '%' would otherwise start an output-template field.
            outtmpl={'default': str(path).replace('%', '%%')},
            progress_hooks=[_ProgressHook(on_progress)],
        )
        with self._factory(options) as downloader:
            downloader.process_ie_result(video.info, download=True)


def _text(value: object) -> str | None:
    """Return a non-empty string value, or None."""
    return value if isinstance(value, str) and value else None
