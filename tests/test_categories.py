"""Tests for failure categorization (DESIGN 5.9)."""

import contextlib
import io
import socket
import ssl
import urllib.error
from http.server import BaseHTTPRequestHandler

import pytest
import requests
import youtube_transcript_api as yta
import yt_dlp
from yt_dlp.extractor.youtube import YoutubeIE
from yt_dlp.utils import ExtractorError, GeoRestrictedError, remove_end

import youtube_collector.categories as testee

VIDEO_ID = 'dQw4w9WgXcQ'
# The legacy handler chains urllib's and ssl's errors, which are OSError subclasses, under yt-dlp's own.
HTTP_ERRORS = (yt_dlp.networking.exceptions.HTTPError, urllib.error.HTTPError)
HANDLERS = pytest.mark.parametrize('compat_opts', [[], ['prefer-legacy-http-handler']], ids=['requests', 'urllib'])


class FakeDownloadError(Exception):
    """Stands in for yt-dlp's DownloadError, which keeps its cause in ``exc_info``."""

    def __init__(self, message, cause=None):
        """Store the message and an ``exc_info`` tuple like yt-dlp's."""
        super().__init__(message)
        self.exc_info = (type(cause), cause, None) if cause is not None else (None, None, None)


def test_category_values_are_the_design_names():
    """The category values are DESIGN 5.9's names."""
    assert [category.value for category in testee.Category] == [
        'unavailable', 'blocked', 'network', 'extractor', 'filesystem', 'internal',
    ]


@pytest.mark.parametrize(('error', 'category'), [
    (yta.RequestBlocked(VIDEO_ID), testee.Category.BLOCKED),
    (yta.IpBlocked(VIDEO_ID), testee.Category.BLOCKED),
    (yta.PoTokenRequired(VIDEO_ID), testee.Category.BLOCKED),
    (yta.AgeRestricted(VIDEO_ID), testee.Category.BLOCKED),
    (yta.FailedToCreateConsentCookie(VIDEO_ID), testee.Category.BLOCKED),
    (yta.VideoUnavailable(VIDEO_ID), testee.Category.UNAVAILABLE),
    (yta.VideoUnplayable(VIDEO_ID, 'Private', []), testee.Category.UNAVAILABLE),
    (yta.InvalidVideoId(VIDEO_ID), testee.Category.UNAVAILABLE),
    (yta.YouTubeDataUnparsable(VIDEO_ID), testee.Category.EXTRACTOR),
    (yta.YouTubeRequestFailed(VIDEO_ID, requests.HTTPError('500 Server Error')), testee.Category.NETWORK),
    (requests.ReadTimeout('read timed out'), testee.Category.NETWORK),
    (requests.ConnectionError('refused'), testee.Category.NETWORK),
    (requests.RequestException('anything'), testee.Category.NETWORK),
    (TimeoutError(), testee.Category.NETWORK),
    (ConnectionResetError(), testee.Category.NETWORK),
    (socket.gaierror(-3, 'Temporary failure in name resolution'), testee.Category.NETWORK),
    (OSError(28, 'No space left on device'), testee.Category.FILESYSTEM),
    (PermissionError(13, 'Permission denied'), testee.Category.FILESYSTEM),
    (FileExistsError(17, 'File exists'), testee.Category.FILESYSTEM),
])
def test_categorize_recognizes_typed_errors(error, category):
    """Caption, requests and standard-library errors are categorized by type."""
    assert testee.categorize(error) is category


@pytest.mark.parametrize(('message', 'category'), [
    ("ERROR: [youtube] x: Sign in to confirm you're not a bot. Use --cookies-from-browser", testee.Category.BLOCKED),
    ('ERROR: [youtube] x: Sign in to confirm your age. This video may be inappropriate', testee.Category.BLOCKED),
    ('ERROR: unable to download video data: HTTP Error 429: Too Many Requests', testee.Category.BLOCKED),
    ('ERROR: [youtube] x: The current session has been rate-limited by YouTube', testee.Category.BLOCKED),
    ('ERROR: [youtube] x: Private video. Sign in if you\'ve been granted access', testee.Category.UNAVAILABLE),
    ('ERROR: [youtube] x: Video unavailable', testee.Category.UNAVAILABLE),
    ('ERROR: [youtube] x: Video unavailable. This video has been removed by the uploader',
     testee.Category.UNAVAILABLE),
    ('ERROR: [youtube] x: Join this channel to get access to members-only content', testee.Category.UNAVAILABLE),
    ('ERROR: [youtube] x: The uploader has not made this video available in your country',
     testee.Category.UNAVAILABLE),
    ('ERROR: [youtube:tab] PLx: The playlist does not exist.', testee.Category.UNAVAILABLE),
    ('ERROR: unable to write data: [Errno 28] No space left on device', testee.Category.FILESYSTEM),
    ('ERROR: unable to open for writing: [Errno 13] Permission denied', testee.Category.FILESYSTEM),
    ('ERROR: [youtube] x: Unable to download API page: <urlopen error timed out>', testee.Category.NETWORK),
    ('ERROR: [youtube] x: Unable to download webpage: Connection reset by peer', testee.Category.NETWORK),
    ('ERROR: unable to download video data: HTTP Error 503: Service Unavailable', testee.Category.NETWORK),
    ('ERROR: [youtube] x: [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred', testee.Category.NETWORK),
    ('ERROR: [youtube] x: Unable to extract yt initial data; please report this issue on GitHub',
     testee.Category.EXTRACTOR),
    ('ERROR: [youtube] x: Requested format is not available. Use --list-formats', testee.Category.EXTRACTOR),
    ('ERROR: [youtube] x: nsig extraction failed: Some formats may be missing', testee.Category.EXTRACTOR),
    ('ERROR: Postprocessing: ffmpeg exited with code 1', testee.Category.INTERNAL),
    ('', testee.Category.INTERNAL),
])
def test_categorize_matches_yt_dlp_messages(message, category):
    """yt-dlp's generic errors are categorized by message, falling back to internal."""
    assert testee.categorize(FakeDownloadError(message)) is category


class SilentLogger:
    """Receives a real YoutubeDL's output, so it writes nothing to the test's streams or logs."""

    def debug(self, message):
        """Discard a message."""

    info = warning = error = debug


def youtube_dl(**options):
    """Return a real YoutubeDL that uses no proxy, retries nothing and logs nowhere."""
    return yt_dlp.YoutubeDL({'logger': SilentLogger(), 'proxy': '', 'retries': 0, 'extractor_retries': 0, **options})


@yt_dlp.YoutubeDL._handle_extraction_exceptions
def _extract_raising(ydl, error):
    """Raise an extractor's error inside yt-dlp's own extraction wrapper, which reports it as a DownloadError."""
    raise error


def reported(error):
    """Return the DownloadError the locked yt-dlp raises when an extractor raises ``error`` during extraction."""
    with youtube_dl() as ydl, pytest.raises(yt_dlp.utils.DownloadError) as raised:
        _extract_raising(ydl, error)
    return raised.value


def unplayable(reason):
    """Return the error the locked yt-dlp raises for a video YouTube reports unplayable with ``reason``.

    Composed as ``extractor/youtube/_video.py`` lines 4071-4073 compose it: a reason that asks to sign in
    loses YouTube's trailing "Learn more" and gains the extractor's real login hint, which names cookies.
    The reason texts are YouTube's, written from memory rather than captured from a live run.
    """
    if 'sign in' in reason.lower():
        reason = remove_end(reason, 'This helps protect our community. Learn more')
        reason = f'{remove_end(reason.strip(), ".")}. {YoutubeIE()._youtube_login_hint}'
    return reported(ExtractorError(reason, expected=True, video_id=VIDEO_ID, ie='youtube'))


@pytest.mark.parametrize(('reason', 'category'), [
    ("Private video. Sign in if you've been granted access to this video", testee.Category.UNAVAILABLE),
    ('Sign in to confirm you\u2019re not a bot. This helps protect our community. Learn more',
     testee.Category.BLOCKED),
    ('Sign in to confirm your age. This video may be inappropriate for some users.', testee.Category.BLOCKED),
    ('This video is available to this channel\'s members on level: Member (or any higher level). Join this '
     'channel to get access to members-only content and other exclusive perks.', testee.Category.UNAVAILABLE),
    ('Video unavailable. This video has been removed by the uploader', testee.Category.UNAVAILABLE),
    ('Premieres in 3 hours', testee.Category.UNAVAILABLE),
    ('This live event will begin in 2 days.', testee.Category.UNAVAILABLE),
    # _video.py lines 4074-4075 append the captcha notice, 4076-4082 the rate-limit explanation.
    ('Video unavailable. YouTube is requiring a captcha challenge before playback', testee.Category.BLOCKED),
    ("This content isn't available, try again later. The current session has been rate-limited by YouTube for "
     'up to an hour. It is recommended to use `-t sleep` to add a delay between video requests to avoid '
     'exceeding the rate limit. For more information, refer to  '
     'https://github.com/yt-dlp/yt-dlp/wiki/Extractors#this-content-isnt-available-try-again-later',
     testee.Category.BLOCKED),
    # Raised directly by _video.py line 3185.
    ('All player responses are invalid. Your IP is likely being blocked by Youtube', testee.Category.BLOCKED),
])
def test_categorize_recognizes_unplayable_videos_as_yt_dlp_reports_them(reason, category):
    """Unplayable videos are categorized from the complete message yt-dlp raises, login hint included."""
    assert testee.categorize(unplayable(reason)) is category


def test_categorize_finds_no_category_in_the_login_hint():
    """yt-dlp's login hint, which names cookies, is advice, not evidence of blocked access."""
    assert testee.categorize(Exception(YoutubeIE()._youtube_login_hint)) is testee.Category.INTERNAL


def test_categorize_recognizes_geo_restriction_by_type():
    """A geo-restricted video is unavailable by its type, whatever its text says; yt-dlp adds advice on proxies."""
    # Without countries, yt-dlp adds no "This video is available in …", which a message pattern would match.
    error = reported(GeoRestrictedError('Restricted in your region'))
    assert 'with --proxy' in str(error)
    assert testee.categorize(error) is testee.Category.UNAVAILABLE


class StatusHandler(BaseHTTPRequestHandler):
    """Answers every request with the status its path names, and no body."""

    def do_GET(self):
        """Send the status in the path."""
        self.send_response(int(self.path.strip('/')))
        self.send_header('Content-Length', '0')
        self.end_headers()

    def log_message(self, format, *args):
        """Keep request logs out of the test's streams."""


@contextlib.contextmanager
def extraction_error(url, **options):
    """Provide the DownloadError a real YoutubeDL raises extracting ``url``, then close its HTTP responses.

    An HTTP error keeps its response open, so leaving it to the garbage collector would warn.
    """
    with youtube_dl(**options) as ydl, pytest.raises(yt_dlp.utils.DownloadError) as raised:
        ydl.extract_info(url, download=False)
    try:
        yield raised.value
    finally:
        for link in testee._chain(raised.value):
            if isinstance(link, HTTP_ERRORS):
                link.close()


@HANDLERS
@pytest.mark.parametrize(('status', 'category'), [
    (429, testee.Category.BLOCKED), (503, testee.Category.NETWORK), (403, testee.Category.INTERNAL),
])
def test_categorize_decides_http_errors_by_their_status(loopback_server, compat_opts, status, category):
    """A real HTTP error is categorized by its status, through either handler, never as filesystem."""
    with extraction_error(f'{loopback_server(StatusHandler)}/{status}', compat_opts=compat_opts) as error:
        assert f'HTTP Error {status}' in str(error)
        assert testee.categorize(error) is category


@HANDLERS
def test_categorize_recognizes_tls_failures_as_network(loopback_server, compat_opts):
    """A real TLS failure is a network error through either handler, although ssl's errors are OSErrors."""
    url = loopback_server(StatusHandler).replace('http://', 'https://')
    with extraction_error(f'{url}/200', compat_opts=compat_opts) as error:
        assert testee.categorize(error) is testee.Category.NETWORK


@HANDLERS
def test_categorize_recognizes_refused_connections_as_network(compat_opts):
    """A real refused connection is a network error through either handler."""
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    with extraction_error(f'http://127.0.0.1:{port}/', compat_opts=compat_opts) as error:
        assert testee.categorize(error) is testee.Category.NETWORK


def test_categorize_recognizes_cookie_load_failures_as_blocked(tmp_path, loopback_server):
    """A profile without cookies is blocked, although yt-dlp chains the failure to a FileNotFoundError."""
    profile = ('firefox', str(tmp_path), None, None)
    with extraction_error(f'{loopback_server(StatusHandler)}/200', cookiesfrombrowser=profile) as error:
        assert 'could not find firefox cookies database' in str(error)
        assert testee.categorize(error) is testee.Category.BLOCKED


@pytest.mark.parametrize('error', [
    ssl.SSLError(1, '[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed'),
    urllib.error.URLError('timed out'),
    yt_dlp.networking.exceptions.TransportError('connection lost'),
])
def test_categorize_recognizes_network_errors_along_the_chain(error):
    """TLS, URL and yt-dlp transport errors are network errors wherever they sit on the chain."""
    assert testee.categorize(FakeDownloadError('ERROR: something went wrong', error)) is testee.Category.NETWORK


def test_categorize_leaves_errors_after_an_http_status_to_the_message():
    """Nothing beyond an HTTP status error decides the category; the message naming the status does."""
    status = urllib.error.HTTPError('http://127.0.0.1/', 429, 'Too Many Requests', {}, io.BytesIO())
    status.__context__ = OSError(28, 'No space left on device')
    with contextlib.closing(status):
        error = FakeDownloadError('ERROR: HTTP Error 429: Too Many Requests', status)
        assert testee.categorize(error) is testee.Category.BLOCKED


def test_categorize_prefers_blocked_over_unavailable_messages():
    """A message naming both a sign-in request and private video is blocked: cookies may fix it."""
    assert testee.categorize(Exception('Private video. Sign in to confirm')) is testee.Category.BLOCKED


def test_categorize_does_not_treat_words_containing_ssl_as_network():
    """The ssl pattern matches the word only."""
    assert testee.categorize(Exception('Tassle failed')) is testee.Category.INTERNAL


def test_categorize_uses_the_type_carried_in_exc_info():
    """A wrapped error is categorized by the original exception yt-dlp keeps in exc_info."""
    error = FakeDownloadError('ERROR: something went wrong', OSError(28, 'No space left on device'))
    assert testee.categorize(error) is testee.Category.FILESYSTEM


def test_categorize_follows_cause_and_context():
    """Explicit and implicit chaining are both followed."""
    try:
        try:
            raise TimeoutError()
        except TimeoutError as inner:
            raise RuntimeError('outer') from inner
    except RuntimeError as caused:
        assert testee.categorize(caused) is testee.Category.NETWORK
    try:
        try:
            raise yta.IpBlocked(VIDEO_ID)
        except yta.IpBlocked:
            raise ValueError('while handling')  # noqa: B904 - implicit chaining is the case under test
    except ValueError as context:
        assert testee.categorize(context) is testee.Category.BLOCKED


def test_categorize_prefers_the_outermost_typed_error():
    """The first typed match along the chain, outermost first, wins."""
    outer = TimeoutError()
    outer.__cause__ = PermissionError(13, 'denied')
    assert testee.categorize(outer) is testee.Category.NETWORK


def test_categorize_terminates_on_cyclic_chains():
    """A chain that refers back to itself is walked once."""
    first = RuntimeError('a')
    second = RuntimeError('b')
    first.__context__ = second
    second.__context__ = first
    assert testee.categorize(first) is testee.Category.INTERNAL


def test_categorize_ignores_malformed_exc_info():
    """An exc_info attribute that is not an exception tuple is ignored."""
    error = RuntimeError('ERROR: timed out')
    error.exc_info = 'not a tuple'
    assert testee.categorize(error) is testee.Category.NETWORK
    error.exc_info = (None, 'not an exception', None)
    assert testee.categorize(error) is testee.Category.NETWORK


@pytest.mark.parametrize('message', ['ERROR: Video unavailable', '  upstream detail\n', ' \t\n'])
def test_failure_message_keeps_the_raw_text(message):
    """The message is the error's own text, unchanged, even when it is only whitespace."""
    assert testee.failure_message(FakeDownloadError(message)) == message


def test_failure_message_falls_back_to_the_type_name():
    """An error without text is described by its type."""
    assert testee.failure_message(TimeoutError()) == 'TimeoutError'
