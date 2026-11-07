"""Parse submitted YouTube URLs into download targets, syntactically and without network access.

DESIGN section 5.2 owns the accepted forms. Only the worker thread may contact YouTube, so the
API validates URLs here by shape alone; whether a video or playlist exists is learned later.
"""

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

__all__ = ['PlaylistTarget', 'UnsupportedUrlError', 'VideoTarget', 'parse_target']

_VIDEO_ID = re.compile(r'[A-Za-z0-9_-]{11}')
_PLAYLIST_ID = re.compile(r'[A-Za-z0-9_-]+')
_YOUTUBE_HOSTS = frozenset({'youtube.com', 'www.youtube.com', 'm.youtube.com'})
_SHORT_HOST = 'youtu.be'
_PATH_VIDEO_PREFIXES = ('live', 'shorts')
_CHANNEL_PREFIXES = ('channel', 'c', 'user')
# YouTube Mix playlists are generated per viewer and effectively unbounded (decision C21).
_MIX_PREFIX = 'RD'


class UnsupportedUrlError(ValueError):
    """Raised when a URL is not a supported single-video or playlist URL; the message is the reason."""


@dataclass(frozen=True)
class VideoTarget:
    """A single video, identified by its 11-character video ID.

    Attributes:
        video_id: The YouTube video ID.
    """

    video_id: str

    @property
    def url(self) -> str:
        """The canonical watch URL, which carries no playlist and so never expands into one."""
        return f'https://www.youtube.com/watch?v={self.video_id}'


@dataclass(frozen=True)
class PlaylistTarget:
    """A playlist, identified by its playlist ID.

    Attributes:
        playlist_id: The YouTube playlist ID; never a Mix (``RD…``) playlist.
    """

    playlist_id: str

    @property
    def url(self) -> str:
        """The canonical playlist URL."""
        return f'https://www.youtube.com/playlist?list={self.playlist_id}'


def parse_target(url: str) -> VideoTarget | PlaylistTarget:
    """Parse a submitted URL into the video or playlist it names.

    A watch URL carrying both ``v`` and ``list`` names the single video; its ``list`` is ignored
    (decision C16). Other query parameters and fragments are ignored too. The host must match
    exactly, so ports and user information are rejected.

    Args:
        url: The URL as submitted.

    Returns:
        The target, whose ``url`` is the canonical form the worker gives yt-dlp.

    Raises:
        UnsupportedUrlError: If the URL is malformed, not YouTube, a channel or handle, a Mix
            playlist, or any other unsupported form; the message says which.
    """
    try:
        parts = urlsplit(url)
    except ValueError as error:
        raise UnsupportedUrlError(f'Malformed URL: {error}') from None
    if parts.scheme.lower() not in ('http', 'https'):
        raise UnsupportedUrlError('Only http and https URLs are supported')
    host = parts.netloc.lower()
    segments = _path_segments(parts.path)
    if host == _SHORT_HOST:
        if len(segments) == 1:
            return _video(segments[0])
        raise UnsupportedUrlError('A youtu.be URL must name exactly one video')
    if host not in _YOUTUBE_HOSTS:
        raise UnsupportedUrlError('Only youtube.com, www.youtube.com, m.youtube.com and youtu.be URLs are supported')
    query = parse_qs(parts.query, keep_blank_values=True)
    match segments:
        case ['watch']:
            return _video(_single_value(query, 'v'))
        case [prefix, video_id] if prefix in _PATH_VIDEO_PREFIXES:
            return _video(video_id)
        case ['playlist']:
            return _playlist(_single_value(query, 'list'))
        case [first, *_] if first in _CHANNEL_PREFIXES or first.startswith('@'):
            raise UnsupportedUrlError('Channel and handle URLs are not supported')
    raise UnsupportedUrlError('Not a supported single-video or playlist URL')


def _path_segments(path: str) -> list[str]:
    """Split a URL path into segments, allowing one trailing slash and nothing else empty."""
    if path.endswith('/'):
        path = path[:-1]
    return path.split('/')[1:] if path.startswith('/') else [path]


def _single_value(query: dict[str, list[str]], name: str) -> str:
    """Return the only value of a query parameter, rejecting a missing or repeated one."""
    values = query.get(name, [])
    if len(values) != 1:
        raise UnsupportedUrlError(f'The URL must carry exactly one {name}= parameter')
    return values[0]


def _video(video_id: str) -> VideoTarget:
    """Build a video target after checking the ID's shape."""
    if not _VIDEO_ID.fullmatch(video_id):
        raise UnsupportedUrlError(f'Not a valid video ID: {video_id!r}')
    return VideoTarget(video_id)


def _playlist(playlist_id: str) -> PlaylistTarget:
    """Build a playlist target after checking the ID's shape and rejecting Mix playlists."""
    if not _PLAYLIST_ID.fullmatch(playlist_id):
        raise UnsupportedUrlError(f'Not a valid playlist ID: {playlist_id!r}')
    if playlist_id.startswith(_MIX_PREFIX):
        raise UnsupportedUrlError('YouTube Mix playlists are not supported')
    return PlaylistTarget(playlist_id)
