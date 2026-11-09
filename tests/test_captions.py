"""Tests for the caption adapter (DESIGN 5.6, D38); no test contacts YouTube."""

from dataclasses import dataclass, field
from unittest.mock import patch

import pytest
import requests
import youtube_transcript_api as yta
from requests.adapters import HTTPAdapter

import youtube_collector.captions as testee

VIDEO = 'abcdefghijk'


@dataclass
class FakeTrack:
    """A listed track that returns a canned transcript when fetched."""

    language_code: str
    is_generated: bool = False
    snippets: list = field(default_factory=list)

    def fetch(self):
        """Return a real FetchedTranscript built from the canned snippets."""
        return yta.FetchedTranscript(
            snippets=[yta.FetchedTranscriptSnippet(*snippet) for snippet in self.snippets], video_id=VIDEO,
            language='English', language_code=self.language_code, is_generated=self.is_generated,
        )


class FakeApi:
    """Stands in for YouTubeTranscriptApi, returning canned tracks or raising."""

    def __init__(self, tracks=(), error=None):
        """Remember the tracks to list or the error to raise."""
        self.tracks = list(tracks)
        self.error = error
        self.listed = []

    def list(self, video_id):
        """Record the video and return its tracks, or raise."""
        self.listed.append(video_id)
        if self.error is not None:
            raise self.error
        return self.tracks


class FakeSession:
    """Records whether it was closed; the real transcript client only sets a header on it here."""

    def __init__(self):
        """Start open."""
        self.headers = {}
        self.closed = 0

    def close(self):
        """Count the close."""
        self.closed += 1


def test_close_closes_the_session_the_client_created():
    """The default session belongs to the client, so the client closes it."""
    session = FakeSession()
    with patch.object(testee, 'timeout_session', return_value=session):
        client = testee.CaptionClient()
    assert session.closed == 0
    client.close()
    assert session.closed == 1


def test_close_leaves_an_injected_client_alone():
    """An injected transcript client stays with whoever made it; no session is created for it."""
    with patch.object(testee, 'timeout_session') as make_session:
        client = testee.CaptionClient(FakeApi())
        client.close()
    make_session.assert_not_called()


def test_timeout_adapter_applies_default_timeouts():
    """A request without a timeout gets 10 s to connect and 30 s of read silence (D38)."""
    with patch.object(HTTPAdapter, 'send', return_value='response') as send:
        assert testee._TimeoutAdapter().send('request') == 'response'
    assert send.call_args.kwargs['timeout'] == (10.0, 30.0)


def test_timeout_adapter_keeps_an_explicit_timeout():
    """A caller's own timeout is respected."""
    with patch.object(HTTPAdapter, 'send', return_value='response') as send:
        testee._TimeoutAdapter().send('request', timeout=5)
    assert send.call_args.kwargs['timeout'] == 5


def test_timeout_session_mounts_the_adapter_for_http_and_https():
    """Every caption request goes through the timeout adapter."""
    session = testee.timeout_session()
    try:
        assert isinstance(session.get_adapter('https://www.youtube.com/'), testee._TimeoutAdapter)
        assert isinstance(session.get_adapter('http://www.youtube.com/'), testee._TimeoutAdapter)
    finally:
        session.close()


def test_timeout_session_applies_defaults_to_requests_without_a_timeout():
    """A plain session.get, as youtube-transcript-api issues it, reaches the adapter with the defaults."""
    session = testee.timeout_session()
    try:
        with patch.object(HTTPAdapter, 'send', return_value=requests.Response()) as send:
            session.get('https://www.youtube.com/watch?v=x')
        assert send.call_args.kwargs['timeout'] == (10.0, 30.0)
    finally:
        session.close()


def test_caption_client_builds_its_api_on_a_timeout_session():
    """The default client uses the timeout session."""
    with patch.object(testee, 'YouTubeTranscriptApi') as api:
        testee.CaptionClient()
    assert isinstance(api.call_args.kwargs['http_client'].get_adapter('https://x/'), testee._TimeoutAdapter)


def test_find_track_selects_from_listed_tracks():
    """The track comes from the listed tracks by DESIGN 5.6's rules: manual first."""
    generated = FakeTrack('zh-CN', True)
    manual = FakeTrack('zh-Hans')
    api = FakeApi([manual, generated])
    assert testee.CaptionClient(api).find_track(VIDEO, ['zh-cn', 'zh']) is manual
    assert api.listed == [VIDEO]


def test_find_track_returns_none_without_a_matching_track():
    """No matching track is a fact, not an error."""
    assert testee.CaptionClient(FakeApi([FakeTrack('fr')])).find_track(VIDEO, ['en']) is None


def test_find_track_returns_none_when_captions_are_disabled():
    """Disabled captions mean no track."""
    api = FakeApi(error=yta.TranscriptsDisabled(VIDEO))
    assert testee.CaptionClient(api).find_track(VIDEO, ['en']) is None


@pytest.mark.parametrize('error', [yta.IpBlocked(VIDEO), requests.ReadTimeout('slow'), yta.VideoUnavailable(VIDEO)])
def test_find_track_propagates_service_errors(error):
    """Caption-service and HTTP errors reach the worker unchanged."""
    with pytest.raises(type(error)):
        testee.CaptionClient(FakeApi(error=error)).find_track(VIDEO, ['en'])


def test_fetch_srt_formats_the_track_as_srt():
    """A fetched track is formatted with SRTFormatter."""
    track = FakeTrack('en', snippets=[('Hello', 0.0, 1.5), ('World', 1.5, 2.0)])
    assert testee.CaptionClient(FakeApi()).fetch_srt(track) == (
        '1\n00:00:00,000 --> 00:00:01,500\nHello\n\n2\n00:00:01,500 --> 00:00:03,500\nWorld\n'
    )
