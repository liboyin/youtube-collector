"""Tests for failure categorization (DESIGN 5.9)."""

import socket

import pytest
import requests
import youtube_transcript_api as yta

import youtube_collector.categories as testee

VIDEO_ID = 'dQw4w9WgXcQ'


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
