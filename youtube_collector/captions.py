"""The caption adapter: list a video's caption tracks, choose one, and fetch it as SRT (DESIGN 5.6).

The adapter reports facts; the worker decides outcomes. Caption HTTP requests keep the source
revision's 10-second connect and 30-second read timeouts (decision D38); the read timeout bounds
silence between bytes, not total transfer time.
"""

from collections.abc import Sequence

import requests
from requests.adapters import HTTPAdapter
from youtube_transcript_api import Transcript, TranscriptsDisabled, YouTubeTranscriptApi
from youtube_transcript_api.formatters import SRTFormatter

from youtube_collector.languages import select_track

__all__ = ['CONNECT_TIMEOUT_SECONDS', 'READ_TIMEOUT_SECONDS', 'CaptionClient', 'timeout_session']

CONNECT_TIMEOUT_SECONDS = 10.0
READ_TIMEOUT_SECONDS = 30.0

# requests' own timeout type, restated because requests keeps it in a private module.
type _Timeout = float | tuple[float | None, float | None] | None


class _TimeoutAdapter(HTTPAdapter):
    """An HTTP adapter that applies the default timeouts to requests that set none."""

    def send(
        self, request: requests.PreparedRequest, stream: bool = False, timeout: _Timeout = None,
        verify: bool | str = True, cert: str | tuple[str, str] | None = None, proxies: dict[str, str] | None = None,
    ) -> requests.Response:
        """Send the request, with the default connect and read timeouts unless it sets its own."""
        if timeout is None:
            timeout = (CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS)
        return super().send(request, stream=stream, timeout=timeout, verify=verify, cert=cert, proxies=proxies)


def timeout_session() -> requests.Session:
    """Return a session whose requests time out after 10 s connecting and 30 s of read silence."""
    session = requests.Session()
    adapter = _TimeoutAdapter()
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session


class CaptionClient:
    """``youtube-transcript-api`` with the collector's timeouts, used only from the worker thread.

    The library's client is not thread-safe, so each instance belongs to one thread.

    Args:
        api: The transcript client; by default one using ``timeout_session``.
    """

    def __init__(self, api: YouTubeTranscriptApi | None = None) -> None:
        """Create the client; nothing contacts YouTube until a method is called."""
        self._api = YouTubeTranscriptApi(http_client=timeout_session()) if api is None else api

    def find_track(self, video_id: str, languages: Sequence[str]) -> Transcript | None:
        """Return the caption track to use, or None when captions are disabled or no track matches.

        Only listed tracks are considered, never translations, so auto-translated tracks are never
        selected (DESIGN 5.6).

        Raises:
            youtube_transcript_api.CouldNotRetrieveTranscript: For caption-service errors other than
                disabled captions.
            requests.RequestException: For HTTP errors, including timeouts.
        """
        try:
            listed = self._api.list(video_id)
        except TranscriptsDisabled:
            return None
        return select_track(languages, list(listed))

    def fetch_srt(self, track: Transcript) -> str:
        """Fetch a track and format it as SRT.

        Raises:
            youtube_transcript_api.CouldNotRetrieveTranscript: For caption-service errors.
            requests.RequestException: For HTTP errors, including timeouts.
        """
        return SRTFormatter().format_transcript(track.fetch())
