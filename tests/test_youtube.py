"""Tests for the yt-dlp adapter (DESIGN 5.3, 5.5, 5.11); no test contacts YouTube."""

import functools
import logging
import threading
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yt_dlp

import youtube_collector.youtube as testee

VIDEO = 'abcdefghijk'


class FakeDownloader:
    """Stands in for ``yt_dlp.YoutubeDL``, recording calls and returning a canned result."""

    def __init__(self, options, result, error=None, progress=()):
        """Record the options and what calls should return or raise."""
        self.options = options
        self.result = result
        self.error = error
        self.progress = progress
        self.calls = []
        self.closed = False

    def __enter__(self):
        """Return the fake."""
        return self

    def __exit__(self, *exc_info):
        """Record that the downloader was closed."""
        self.closed = True

    def extract_info(self, url, download=True, process=True):
        """Record the call and return the canned result or raise."""
        self.calls.append(('extract_info', url, download, process))
        if self.error is not None:
            raise self.error
        return self.result

    def process_ie_result(self, ie_result, download=True):
        """Record the call and replay canned progress reports through the hooks."""
        self.calls.append(('process_ie_result', ie_result, download))
        for status in self.progress:
            for hook in self.options['progress_hooks']:
                hook(status)
        return ie_result


class Factory:
    """Build fakes and remember them."""

    def __init__(self, **keywords):
        """Remember the fake's behavior."""
        self.keywords = keywords
        self.made = []

    def __call__(self, options):
        """Make a fake for these options."""
        self.made.append(FakeDownloader(options, **self.keywords))
        return self.made[-1]


def client(factory, cookies=False, pacing=None):
    """Return a client using the fake factory."""
    return testee.YoutubeClient(cookies=cookies, pacing=pacing or testee.Pacing(), factory=factory)


def test_firefox_profile_roots_lists_yt_dlp_locations(tmp_path):
    """The roots match yt-dlp's Linux Firefox locations, including the snap path Compose mounts."""
    roots = testee.firefox_profile_roots(tmp_path)
    assert tmp_path / 'snap/firefox/common/.mozilla/firefox' in roots
    assert roots[0] == tmp_path / '.config/mozilla/firefox'
    assert testee.firefox_profile_roots(tmp_path, tmp_path / 'xdg')[0] == tmp_path / 'xdg/mozilla/firefox'


@pytest.mark.parametrize('relative', ['cookies.sqlite', 'abc.default/cookies.sqlite', 'Profiles/x/cookies.sqlite'])
def test_firefox_cookies_present_finds_profile_databases(tmp_path, relative):
    """A cookies.sqlite at any of yt-dlp's patterns counts (DESIGN 5.11)."""
    root = tmp_path / 'firefox'
    (root / relative).parent.mkdir(parents=True, exist_ok=True)
    (root / relative).touch()
    assert testee.firefox_cookies_present([tmp_path / 'missing', root])


def test_firefox_cookies_present_reports_absence(tmp_path):
    """Without a cookies.sqlite, including deeper ones, cookies are off."""
    (tmp_path / 'a/b/c').mkdir(parents=True)
    (tmp_path / 'a/b/c/cookies.sqlite').touch()
    (tmp_path / 'a/other.sqlite').touch()
    assert not testee.firefox_cookies_present([tmp_path / 'a', tmp_path / 'missing'])


def test_yt_dlp_logger_routes_messages_to_logging(caplog):
    """yt-dlp output goes to logging: debug and info at debug level, warnings and errors as such."""
    caplog.set_level(logging.DEBUG, logger=testee.__name__)
    adapter = testee._YtDlpLogger()
    adapter.debug('d')
    adapter.info('i')
    adapter.warning('w')
    adapter.error('e')
    assert [(r.levelno, r.message) for r in caplog.records] == [
        (logging.DEBUG, 'd'), (logging.DEBUG, 'i'), (logging.WARNING, 'w'), (logging.ERROR, 'e'),
    ]


def test_options_use_deno_quiet_logging_and_configured_pacing():
    """Every request uses Deno, routes output to logging and takes pacing from configuration (DESIGN 5.3, 5.5)."""
    options = client(Factory(result={}), pacing=testee.Pacing(1.5, 2.0, 4.0))._options(extra=1)
    assert options['js_runtimes'] == {'deno': {}}
    assert (options['quiet'], options['noprogress']) == (True, True)
    assert isinstance(options['logger'], testee._YtDlpLogger)
    assert (options['sleep_interval_requests'], options['sleep_interval'], options['max_sleep_interval']) == (
        1.5, 2.0, 4.0,
    )
    assert options['extra'] == 1
    assert 'cookiesfrombrowser' not in options


def test_options_omit_max_sleep_without_a_range_and_add_cookies_when_detected():
    """Firefox cookies are used only when detected; a fixed sleep has no upper bound."""
    options = client(Factory(result={}), cookies=True)._options()
    assert options['cookiesfrombrowser'] == ('firefox',)
    assert 'max_sleep_interval' not in options


def test_options_are_accepted_by_yt_dlp():
    """The real YoutubeDL accepts the options, including the JavaScript runtime, without network access."""
    options = client(Factory(result={}))._options(noplaylist=True, format=testee.MEDIA_FORMAT)
    with yt_dlp.YoutubeDL(options) as downloader:
        assert downloader.params['js_runtimes'] == {'deno': {}}


def test_expand_playlist_lists_entries_in_order_and_skips_empty_ones():
    """Flat entries become videos in order; empty and ID-less entries are skipped (DESIGN 5.3)."""
    factory = Factory(result={'id': 'PL', 'title': 'List', 'entries': [
        {'id': 'a' * 11, 'title': 'A'}, None, {'title': 'no id'}, {'id': ''}, {'id': 'b' * 11},
    ]})
    playlist = client(factory).expand_playlist('https://www.youtube.com/playlist?list=PL')
    assert playlist == testee.Playlist(
        'List', (testee.PlaylistVideo('a' * 11, 'A'), testee.PlaylistVideo('b' * 11, None)),
    )
    [made] = factory.made
    assert made.options['extract_flat'] == 'in_playlist'
    assert made.calls == [('extract_info', 'https://www.youtube.com/playlist?list=PL', False, True)]
    assert made.closed


@pytest.mark.parametrize(('result', 'title'), [({'id': 'PL'}, 'PL'), ({}, 'url'), ({'entries': 'bad'}, 'url')])
def test_expand_playlist_falls_back_for_missing_titles_and_entries(result, title):
    """A missing title falls back to the ID, then the URL; a malformed entry list counts as empty."""
    playlist = client(Factory(result=result)).expand_playlist('url')
    assert (playlist.title, playlist.entries) == (title, ())


def test_expand_playlist_rejects_an_empty_result():
    """yt-dlp returning nothing is an error, not an empty playlist."""
    with pytest.raises(ValueError):
        client(Factory(result=None)).expand_playlist('url')


def test_expand_playlist_propagates_yt_dlp_errors():
    """yt-dlp's errors reach the worker unchanged."""
    error = yt_dlp.utils.DownloadError('ERROR: The playlist does not exist.')
    with pytest.raises(yt_dlp.utils.DownloadError) as raised:
        client(Factory(result=None, error=error)).expand_playlist('url')
    assert raised.value is error


def test_video_info_extracts_once_without_processing():
    """Metadata is extracted without format selection, for one video only."""
    info = {'id': VIDEO, 'title': 'Title', 'formats': []}
    factory = Factory(result=info)
    video = client(factory).video_info('watch-url')
    assert (video.video_id, video.title, video.info) == (VIDEO, 'Title', info)
    [made] = factory.made
    assert made.options['noplaylist'] is True
    assert 'format' not in made.options
    assert made.calls == [('extract_info', 'watch-url', False, False)]


def test_video_info_tolerates_a_missing_title():
    """A video without a title gets an empty one, which naming later rejects."""
    assert client(Factory(result={'id': VIDEO})).video_info('u').title == ''


@pytest.mark.parametrize('result', [None, {'title': 'x'}, {'id': ''}])
def test_video_info_rejects_results_without_a_video(result):
    """No result or no ID is an error."""
    with pytest.raises(ValueError):
        client(Factory(result=result)).video_info('u')


def test_download_media_processes_the_extracted_info_with_media_options(tmp_path):
    """The download reuses the extraction, selects MP4 formats and writes to the literal path (DESIGN 5.5)."""
    activity = []
    factory = Factory(result=None, progress=[{'status': 'downloading'}, {'status': 'finished'}])
    video = testee.VideoInfo(VIDEO, 'Title', {'id': VIDEO})
    path = tmp_path / '100% [x]' / 'a.mp4'
    client(factory, cookies=True).download_media(video, path, lambda: activity.append(1))
    [made] = factory.made
    assert made.calls == [('process_ie_result', video.info, True)]
    assert made.options['format'] == 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]'
    assert made.options['merge_output_format'] == 'mp4'
    assert made.options['outtmpl'] == {'default': str(path).replace('%', '%%')}
    assert made.options['cookiesfrombrowser'] == ('firefox',)
    assert len(activity) == 2  # every yt-dlp report counts as activity, for the marker heartbeat (C43)


class QuietHandler(SimpleHTTPRequestHandler):
    """Serve files from the test's directory without logging to stderr."""

    def log_message(self, format, *args):  # noqa: A002 - the base class names the parameter
        """Discard the access log."""


class LoopbackServer(ThreadingHTTPServer):
    """A loopback HTTP server whose ``server_close`` joins every request handler.

    ``ThreadingHTTPServer`` runs handlers on daemon threads, which ``server_close`` does not join, so a
    handler could outlive the test that owns its files. Non-daemon handler threads are joined.
    """

    daemon_threads = False


@pytest.fixture
def local_server(tmp_path):
    """Serve ``tmp_path/served`` on loopback for the test; teardown stops the server and joins every handler."""
    served = tmp_path / 'served'
    served.mkdir()
    server = LoopbackServer(('127.0.0.1', 0), functools.partial(QuietHandler, directory=str(served)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield served, f'http://127.0.0.1:{server.server_address[1]}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_loopback_server_close_waits_for_request_handlers(tmp_path):
    """Closing the test server joins a handler that is still finishing, so no handler outlives its test."""
    (tmp_path / 'file').write_bytes(b'data')
    entered = threading.Event()
    release = threading.Event()
    order = []
    client_errors = []
    handler_threads = []

    class BlockingHandler(QuietHandler):
        """A handler that holds its connection open until released."""

        def finish(self):
            """Record this handler's thread, signal entry, wait for release, then finish."""
            handler_threads.append(threading.current_thread())  # joined in teardown whatever daemon_threads says
            entered.set()
            release.wait(10)
            super().finish()
            order.append('handler finished')

    def fetch():
        try:
            urllib.request.urlopen(f'http://127.0.0.1:{server.server_address[1]}/file', timeout=10).close()
        except Exception as error:  # recorded here, so the test owns the client's failure too
            client_errors.append(error)

    def close():
        server.server_close()
        order.append('server closed')

    server = LoopbackServer(('127.0.0.1', 0), functools.partial(BlockingHandler, directory=str(tmp_path)))
    serving = threading.Thread(target=server.serve_forever)
    client = threading.Thread(target=fetch)
    closer = threading.Thread(target=close)
    serving.start()
    try:
        client.start()
        assert entered.wait(10)
        server.shutdown()
        closer.start()
        # A bounded negative check: with daemon handler threads, server_close would return at once.
        closer.join(0.5)
        assert closer.is_alive()
        release.set()
        closer.join(10)
        assert order == ['handler finished', 'server closed']
    finally:
        # Unconditional teardown, also when an assertion above failed: release the handler, stop the
        # server, close it (which joins the handler) unless the closer already does, and join everything.
        release.set()
        server.shutdown()
        if closer.ident is None:
            server.server_close()
        threads = [*handler_threads, *(thread for thread in (closer, client, serving) if thread.ident is not None)]
        for thread in threads:
            thread.join(10)
        assert not any(thread.is_alive() for thread in threads)
    assert client_errors == []


def test_download_media_writes_to_a_literal_path_with_real_yt_dlp(tmp_path, local_server):
    """Real yt-dlp downloads an unprocessed extraction to a path containing template characters (DESIGN 5.5).

    The formats are served by a test-owned loopback server, so nothing reaches YouTube.
    """
    served, base_url = local_server
    (served / 'video.mp4').write_bytes(b'x' * 4096)
    (served / 'video.webm').write_bytes(b'y' * 8192)
    info = {
        'id': VIDEO, 'title': 'T', 'extractor': 'youtube', 'extractor_key': 'Youtube',
        'webpage_url': f'https://www.youtube.com/watch?v={VIDEO}',
        'formats': [
            {'format_id': '18', 'url': f'{base_url}/video.mp4', 'ext': 'mp4', 'vcodec': 'avc1', 'acodec': 'mp4a',
             'protocol': 'http'},
            {'format_id': '43', 'url': f'{base_url}/video.webm', 'ext': 'webm', 'vcodec': 'vp8', 'acodec': 'vorbis',
             'protocol': 'http', 'quality': 10},
        ],
    }
    path = tmp_path / 'stage %(title)s 100%' / 'media.mp4'
    path.parent.mkdir()
    reports = []
    youtube = client(yt_dlp.YoutubeDL)
    youtube.download_media(testee.VideoInfo(VIDEO, 'T', info), path, lambda: reports.append(1))
    assert path.read_bytes() == b'x' * 4096
    assert sorted(p.name for p in path.parent.iterdir()) == ['media.mp4']
    assert reports  # real yt-dlp reports reach the callback


def test_text_accepts_non_empty_strings_only():
    """Only non-empty strings count as text."""
    assert [testee._text(v) for v in ('a', '', None, 3)] == ['a', None, None, None]
