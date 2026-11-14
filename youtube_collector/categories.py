"""Categorize errors into DESIGN 5.9's failure categories.

Categories are a hint at whether resubmitting helps; the raw message stays authoritative. yt-dlp
raises mostly one generic error type, so its errors are recognized by message. Typed errors from
``youtube-transcript-api``, ``requests``, yt-dlp's networking and the standard library are recognized
by type first, anywhere along the error's chain.
"""

import re
import socket
import ssl
import urllib.error
from collections.abc import Iterator
from enum import StrEnum

import requests
import yt_dlp.cookies
import yt_dlp.networking.exceptions
import yt_dlp.utils
from youtube_transcript_api import (
    AgeRestricted,
    FailedToCreateConsentCookie,
    InvalidVideoId,
    PoTokenRequired,
    RequestBlocked,
    VideoUnavailable,
    VideoUnplayable,
    YouTubeDataUnparsable,
    YouTubeRequestFailed,
)

__all__ = ['Category', 'categorize', 'failure_message']


class Category(StrEnum):
    """Why something failed, as a hint at whether resubmitting helps.

    Attributes:
        UNAVAILABLE: Private, removed, members-only or geo-blocked, which is permanent; or a scheduled
            premiere or live event, which resubmitting after its start fixes.
        BLOCKED: Bot check, captcha, sign-in required, rate limiting, or cookies rejected or not loadable.
            Fix cookies, then resubmit.
        NETWORK: Timeouts and connection errors. Resubmit as is.
        EXTRACTOR: yt-dlp failed to parse YouTube's response. Usually fixed by updating yt-dlp.
        FILESYSTEM: A staging write or publication failed.
        INTERNAL: Anything else.
    """

    UNAVAILABLE = 'unavailable'
    BLOCKED = 'blocked'
    NETWORK = 'network'
    EXTRACTOR = 'extractor'
    FILESYSTEM = 'filesystem'
    INTERNAL = 'internal'


# An HTTP error's status decides its category, and the outer message names it ("HTTP Error 429"), so
# the walk along the chain stops here and the message patterns decide. urllib's HTTPError is an OSError
# and would otherwise reach the filesystem catch-all.
_HTTP_STATUS_ERRORS = (yt_dlp.networking.exceptions.HTTPError, urllib.error.HTTPError)

_TYPED: tuple[tuple[tuple[type[BaseException], ...], Category], ...] = (
    ((RequestBlocked, PoTokenRequired, AgeRestricted, FailedToCreateConsentCookie), Category.BLOCKED),
    # yt-dlp wraps any failure to load cookies in CookieLoadError, chained to the FileNotFoundError or
    # other error that caused it, which must not reach the filesystem catch-all.
    ((yt_dlp.cookies.CookieLoadError,), Category.BLOCKED),
    ((VideoUnavailable, VideoUnplayable, InvalidVideoId, yt_dlp.utils.GeoRestrictedError), Category.UNAVAILABLE),
    ((YouTubeDataUnparsable,), Category.EXTRACTOR),
    ((YouTubeRequestFailed, requests.RequestException, yt_dlp.networking.exceptions.TransportError), Category.NETWORK),
    # Network errors are OSError subclasses, so they are matched before the filesystem catch-all.
    ((TimeoutError, ConnectionError, socket.gaierror, ssl.SSLError, urllib.error.URLError), Category.NETWORK),
    ((OSError,), Category.FILESYSTEM),
)

# Tried in this order on the outermost message; the first matching category wins. There is no pattern for
# "cookies": yt-dlp appends a hint naming them to sign-in reasons that cookies cannot fix, such as a
# private video's, and cookie-load failures are recognized by type.
_PATTERNS: tuple[tuple[Category, re.Pattern[str]], ...] = tuple(
    (category, re.compile('|'.join(patterns), re.IGNORECASE)) for category, patterns in (
        (Category.BLOCKED, (
            r'sign in to confirm', r'not a bot', r'http error 429', r'too many requests', r'rate.?limit',
            r'login required', r'sign in to view', r'captcha', r'ip is likely being blocked',
        )),
        (Category.UNAVAILABLE, (
            r'private video', r'video unavailable', r'this video (?:has been removed|is (?:no longer )?available|'
            r'is not available)', r'members.?only', r'join this channel', r'not (?:made this video )?available in '
            r'your country', r'geo.?restrict', r'has been terminated', r'no longer available',
            r'playlist does not exist', r'removed by the uploader', r'copyright claim', r'premieres in',
            r'live event will begin',
        )),
        (Category.FILESYSTEM, (
            r'no space left', r'permission denied', r'unable to write', r'unable to open for writing',
            r'read-only file system', r'unable to rename file', r'disk quota',
        )),
        (Category.NETWORK, (
            r'timed out', r'timeout', r'connection (?:reset|refused|aborted)', r'network is unreachable',
            r'temporary failure in name resolution', r'name or service not known', r'incompleteread',
            r'remote end closed', r'urlopen error', r'\bssl\b', r'getaddrinfo failed', r'http error 5\d\d',
        )),
        (Category.EXTRACTOR, (
            r'unable to extract', r'failed to parse', r'signature', r'nsig', r'requested format is not available',
            r'unsupported url', r'player response', r'javascript', r'please report this issue',
        )),
    )
)


def categorize(error: BaseException) -> Category:
    """Return the failure category for an error.

    Typed rules are tried along the error's chain first, outermost first, until an HTTP error that
    carries a status; then message patterns on the outermost error; then ``INTERNAL``.
    """
    for link in _chain(error):
        if isinstance(link, _HTTP_STATUS_ERRORS):
            break
        for types, category in _TYPED:
            if isinstance(link, types):
                return category
    message = str(error)
    for category, pattern in _PATTERNS:
        if pattern.search(message):
            return category
    return Category.INTERNAL


def failure_message(error: BaseException) -> str:
    """Return an error's raw text for a failure record, unchanged, or its type name when it is empty.

    The text is kept exactly, whitespace included, because it is the authoritative detail (DESIGN 5.9).
    """
    return str(error) or type(error).__name__


def _chain(error: BaseException) -> Iterator[BaseException]:
    """Yield the error and its causes: yt-dlp's ``exc_info``, ``__cause__`` and ``__context__``.

    yt-dlp's ``DownloadError`` keeps the original exception in an ``exc_info`` tuple instead of
    chaining it. Each error is yielded once, so a cyclic chain terminates.
    """
    seen: set[int] = set()
    pending: list[BaseException] = [error]
    while pending:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        exc_info = getattr(current, 'exc_info', None)
        if isinstance(exc_info, tuple) and len(exc_info) > 1 and isinstance(exc_info[1], BaseException):
            pending.append(exc_info[1])
        pending.extend(link for link in (current.__cause__, current.__context__) if link is not None)
