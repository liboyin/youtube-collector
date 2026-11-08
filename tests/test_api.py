"""Tests for the HTTP API and the service's composition (DESIGN 5.1, 5.8-5.10, 5.14, section 6).

The real lifespan runs, with the worker thread, against a test-owned staging directory and database
and fake adapters, so nothing reaches YouTube.
"""

import sqlite3
import threading
import time
from dataclasses import dataclass
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import youtube_collector.api as testee
from youtube_collector import staging
from youtube_collector.config import Settings
from youtube_collector.youtube import Playlist, PlaylistVideo, VideoInfo

VIDEO = 'abcdefghijk'
OTHER = 'bbbbbbbbbbb'
VIDEO_URL = f'https://youtu.be/{VIDEO}'
PLAYLIST_URL = 'https://www.youtube.com/playlist?list=PLlist'


@dataclass(frozen=True)
class Track:
    """A listed caption track."""

    language_code: str
    is_generated: bool = False


class FakeMedia:
    """A yt-dlp adapter that writes small files and can be told to fail."""

    def __init__(self):
        """Know two videos and a two-entry playlist."""
        self.download_error = None
        self.titles = {VIDEO: 'A video', OTHER: 'Other'}

    def expand_playlist(self, url):
        """Return a two-entry playlist."""
        return Playlist('A list', (PlaylistVideo(VIDEO, None), PlaylistVideo(OTHER, None)))

    def video_info(self, url):
        """Return metadata for the video in the URL."""
        video_id = url.rsplit('=', 1)[1]
        return VideoInfo(video_id, self.titles[video_id], {'id': video_id})

    def download_media(self, video, path, on_activity):
        """Write fake media, or raise the configured error."""
        on_activity()
        if self.download_error is not None:
            raise self.download_error
        path.write_bytes(b'media')


class FakeCaptions:
    """A caption adapter with one English track."""

    def find_track(self, video_id, languages):
        """Return the English track."""
        return Track('en')

    def fetch_srt(self, track):
        """Return SRT text."""
        return '1\n00:00:00,000 --> 00:00:01,000\nhi\n'


class Monotonic:
    """A monotonic clock the test can advance; it starts from the real one's scale."""

    def __init__(self):
        """Start at zero."""
        self.now = 0.0

    def __call__(self):
        """Return the current time."""
        return self.now


@pytest.fixture
def staging_root(tmp_path):
    """Provide a staging root with its marker."""
    root = tmp_path / 'staging'
    root.mkdir()
    (root / staging.MARKER_NAME).touch()
    return root


@pytest.fixture
def media():
    """Provide the fake yt-dlp adapter."""
    return FakeMedia()


@pytest.fixture
def monotonic():
    """Provide the injected monotonic clock."""
    return Monotonic()


@pytest.fixture
def settings(tmp_path, staging_root):
    """Provide settings whose every path is test-owned."""
    return Settings(
        staging_dir=staging_root, database=tmp_path / 'db.sqlite3', job_retention=3600, staging_health_max_age=60,
        shutdown_grace=10,
    )


def make_client(settings, media, monotonic):
    """Return a TestClient for an app with fake components; entering it runs the lifespan."""
    components = testee.Components(media=media, captions=FakeCaptions(), monotonic=monotonic)
    return TestClient(testee.create_app(settings, lambda _settings: components))


@pytest.fixture
def client(settings, media, monotonic):
    """Provide a running service; leaving the fixture stops its threads."""
    with make_client(settings, media, monotonic) as running:
        yield running


def wait_for(condition):
    """Wait for a condition set by the worker thread, failing the test after a bound."""
    deadline = time.monotonic() + 10
    while not condition():
        assert time.monotonic() < deadline, 'condition was not reached'
        time.sleep(0.005)


def wait_until_finished(client, job):
    """Poll a job until it is finished, and return it."""
    seen = {}

    def finished():
        response = client.get(f'/jobs/{job["id"]}')
        assert response.status_code == 200
        seen.update(response.json())
        return seen['state'] == 'finished'

    wait_for(finished)
    return seen


def wait_for_staging_check(client):
    """Wait for the worker's first marker check."""
    wait_for(lambda: client.app.state.runtime.worker.staging_check() is not None)


def test_default_components_detect_cookies_once_and_build_the_real_adapters(settings):
    """The default factory builds the real adapters with the cookie detection result (DESIGN 5.11)."""
    with patch.object(testee, 'firefox_cookies_present', return_value=False) as detect, \
            patch.object(testee, 'firefox_profile_roots', return_value=()):
        components = testee.default_components(settings)
    detect.assert_called_once_with(())
    assert isinstance(components.media, testee.YoutubeClient)
    assert isinstance(components.captions, testee.CaptionClient)


def test_submit_videos_returns_202_and_wakes_the_worker(settings, media, monotonic, staging_root):
    """A submission is accepted at once and wakes the idle worker, which then publishes the file (DESIGN 5.1)."""
    idle = threading.Event()
    real_wait = testee.Worker._wait

    def wait(worker, timeout):
        idle.set()
        real_wait(worker, timeout)

    with patch.object(testee.Worker, '_wait', wait), make_client(settings, media, monotonic) as client:
        assert idle.wait(10)  # the worker now waits up to 30 s for work, far longer than wait_for's bound
        response = client.post('/jobs/videos', json={'url': VIDEO_URL})
        assert response.status_code == 202
        job = response.json()
        assert (job['kind'], job['state'], job['target']) == ('videos', 'queued', {'type': 'video', 'id': VIDEO})
        finished = wait_until_finished(client, job)
    assert finished['items'][0]['media'] == {'outcome': 'downloaded', 'path': f'{VIDEO} - A video.mp4'}
    assert (staging_root / f'{VIDEO} - A video.mp4').read_bytes() == b'media'


def test_submit_videos_with_captions_writes_both(client, staging_root):
    """A combined job writes the MP4 and its SRT."""
    job = client.post('/jobs/videos-with-captions', json={'url': VIDEO_URL, 'languages': ['en']}).json()
    finished = wait_until_finished(client, job)
    assert finished['items'][0]['captions']['outcome'] == 'downloaded'
    assert (staging_root / f'{VIDEO} - A video.srt').exists()


def test_submit_captions_expands_playlists(client, staging_root):
    """A captions job on a playlist writes into its playlist folder."""
    job = client.post('/jobs/captions', json={'url': PLAYLIST_URL, 'languages': ['en-US', 'en']}).json()
    finished = wait_until_finished(client, job)
    assert [item['captions']['path'] for item in finished['items']] == [
        f'PLlist - A list/{VIDEO} - A video.srt', f'PLlist - A list/{OTHER} - Other.srt',
    ]


@pytest.mark.parametrize(('path', 'body', 'message'), [
    ('/jobs/videos', {'url': 'https://www.youtube.com/@someone'}, 'Channel'),
    ('/jobs/videos', {'url': 'https://www.youtube.com/playlist?list=RDabc'}, 'Mix'),
    ('/jobs/videos', {'url': VIDEO_URL, 'languages': ['en']}, 'Extra inputs'),
    ('/jobs/videos', {}, 'Field required'),
    ('/jobs/captions', {'url': VIDEO_URL}, 'Field required'),
    ('/jobs/captions', {'url': VIDEO_URL, 'languages': []}, 'at least 1'),
    ('/jobs/videos-with-captions', {'url': VIDEO_URL, 'languages': ['en', 'en_US']}, 'BCP 47'),
    ('/jobs/videos', {'url': VIDEO_URL, 'idempotency_key': 'k'}, 'Extra inputs'),
])
def test_invalid_submissions_return_422(client, path, body, message):
    """Malformed bodies, unsupported URLs, channels and bad language tags are rejected (DESIGN 5.1)."""
    response = client.post(path, json=body)
    assert response.status_code == 422
    assert message in response.text
    assert client.get('/jobs').json()['jobs'] == []


def test_a_repeated_submission_is_a_new_job_that_finds_the_files(client):
    """Without idempotency keys a repeat is a second 202, and its job reports the file as existing (C46)."""
    body = {'url': VIDEO_URL}
    first = client.post('/jobs/videos', json=body)
    wait_until_finished(client, first.json())
    second = client.post('/jobs/videos', json=body)
    assert (first.status_code, second.status_code) == (202, 202)
    assert second.json()['id'] != first.json()['id']
    assert wait_until_finished(client, second.json())['items'][0]['media']['outcome'] == 'existing'


def test_list_jobs_returns_every_retained_job(client):
    """GET /jobs is the status table and nothing else (DESIGN 5.8)."""
    job = client.post('/jobs/videos', json={'url': VIDEO_URL}).json()
    finished = wait_until_finished(client, job)
    assert client.get('/jobs').json() == {'jobs': [finished]}


def test_a_finished_job_stays_readable_after_its_file_is_moved(client, staging_root):
    """Records do not follow the files: moving a file changes nothing (C40)."""
    job = client.post('/jobs/videos', json={'url': VIDEO_URL}).json()
    finished = wait_until_finished(client, job)
    (staging_root / f'{VIDEO} - A video.mp4').unlink()
    assert client.get(f'/jobs/{job["id"]}').json() == finished
    assert [listed['id'] for listed in client.get('/jobs').json()['jobs']] == [job['id']]


def test_a_new_submission_deletes_jobs_past_the_retention(settings, media, monotonic):
    """The configured retention reaches the store: an expired job is 404 after the next submission (C42)."""
    settings = Settings(**{**settings.__dict__, 'job_retention': 0})
    with make_client(settings, media, monotonic) as client:
        job = client.post('/jobs/videos', json={'url': VIDEO_URL}).json()
        wait_until_finished(client, job)
        assert client.get(f'/jobs/{job["id"]}').status_code == 200
        client.post('/jobs/videos', json={'url': f'https://youtu.be/{OTHER}'})
        assert client.get(f'/jobs/{job["id"]}').status_code == 404


def test_a_job_within_the_retention_survives_a_new_submission(client):
    """The configured retention is honored in the other direction too."""
    job = client.post('/jobs/videos', json={'url': VIDEO_URL}).json()
    wait_until_finished(client, job)
    client.post('/jobs/videos', json={'url': f'https://youtu.be/{OTHER}'})
    assert client.get(f'/jobs/{job["id"]}').status_code == 200


def test_get_job_returns_404_for_unknown_jobs(client):
    """An unknown job is 404."""
    assert client.get('/jobs/missing').status_code == 404


def test_the_change_feed_route_is_gone(client):
    """/jobs/changes is no longer a route: it reads as a job with that ID (C39)."""
    response = client.get('/jobs/changes', params={'since': 0, 'wait': 0})
    assert response.status_code == 404
    assert response.json()['detail'] == 'Unknown job changes'


def test_get_job_ignores_long_poll_parameters_and_returns_at_once(client):
    """There is no long polling: the old parameters change nothing (C41)."""
    job = client.post('/jobs/videos', json={'url': VIDEO_URL}).json()
    finished = wait_until_finished(client, job)
    assert client.get(f'/jobs/{job["id"]}', params={'since': 10 ** 9, 'wait': 60}).json() == finished


def test_failures_are_listed_embedded_and_cleared(client, media):
    """Failures appear with their reason, in their job too, and clear one by one or at once (DESIGN 5.9)."""
    media.download_error = RuntimeError("ERROR: Sign in to confirm you're not a bot")
    jobs = [client.post('/jobs/videos', json={'url': url}).json() for url in (VIDEO_URL, f'https://youtu.be/{OTHER}')]
    finished = [wait_until_finished(client, job) for job in jobs]
    listed = client.get('/failures').json()
    assert set(listed) == {'failures'}
    assert [f['category'] for f in listed['failures']] == ['blocked', 'blocked']
    assert [job['failures'] for job in finished] == [[listed['failures'][0]], [listed['failures'][1]]]
    first = listed['failures'][0]['id']
    cleared = client.delete(f'/failures/{first}')
    assert (cleared.status_code, cleared.json()) == (200, listed['failures'][0])
    assert client.delete(f'/failures/{first}').status_code == 404
    assert client.get(f'/jobs/{jobs[0]["id"]}').json()['failures'] == []
    assert client.delete('/failures').json() == {'failures': [listed['failures'][1]]}
    assert client.delete('/failures').json() == {'failures': []}
    assert client.get('/failures').json() == {'failures': []}


def test_health_is_200_when_everything_is_healthy(client):
    """A healthy service reports 200 with every component and the yt-dlp version (DESIGN 5.14)."""
    wait_for_staging_check(client)
    response = client.get('/health')
    body = response.json()
    assert response.status_code == 200
    assert body['healthy'] is True
    assert body['database'] == {'ok': True, 'error': None}
    assert (body['staging']['ok'], body['staging']['marker_present'], body['staging']['paused']) == (True, True, False)
    assert body['threads'] == {'worker': True}
    assert body['yt_dlp_version'] == testee.yt_dlp.version.__version__


def test_health_is_503_while_the_marker_is_missing(client, staging_root):
    """A missing marker makes the service unhealthy and reports work as paused (5.13, 5.14)."""
    wait_for_staging_check(client)
    (staging_root / staging.MARKER_NAME).unlink()
    worker = client.app.state.runtime.worker
    worker.wake()
    wait_for(lambda: not worker.staging_check().marker_present)
    response = client.get('/health')
    assert response.status_code == 503
    assert response.json()['staging'] == {'ok': False, 'marker_present': False,
                                          'checked_seconds_ago': 0.0, 'paused': True}


def test_health_is_503_when_the_staging_check_is_stale(client, monotonic):
    """A cached staging check older than the limit counts as unhealthy (C35)."""
    wait_for_staging_check(client)
    monotonic.now += 60
    assert client.get('/health').status_code == 200  # exactly at the limit is still fresh
    monotonic.now += 1
    response = client.get('/health')
    assert response.status_code == 503
    assert response.json()['staging']['checked_seconds_ago'] == 61.0


def test_health_is_503_when_the_database_fails(client):
    """A database that cannot be locked for writing is unhealthy."""
    wait_for_staging_check(client)
    with patch.object(testee.Store, 'check', side_effect=sqlite3.OperationalError('database is locked')):
        response = client.get('/health')
    assert response.status_code == 503
    assert response.json()['database'] == {'ok': False, 'error': 'database is locked'}


def test_health_is_503_while_the_api_cannot_open_the_database_and_recovers(settings, media, monotonic):
    """A failure to open the API's own connection is a 503 with the normal body, retried on the next call."""
    real_store = testee.Store
    failing = threading.Event()
    failing.set()

    def open_store(*arguments, **keywords):
        if failing.is_set() and threading.current_thread().name.startswith('api-db'):
            raise sqlite3.OperationalError('unable to open database file')
        return real_store(*arguments, **keywords)

    with patch.object(testee, 'Store', side_effect=open_store):
        with make_client(settings, media, monotonic) as client:
            # A request needing the database fails (a generic 500 in production; TestClient re-raises it),
            # and the failure must not break later calls.
            with pytest.raises(sqlite3.OperationalError):
                client.get('/jobs')
            response = client.get('/health')
            assert response.status_code == 503
            assert response.json()['database'] == {'ok': False, 'error': 'unable to open database file'}
            failing.clear()
            assert client.get('/jobs').status_code == 200
            wait_for_staging_check(client)
            assert client.get('/health').status_code == 200
        runtime = client.app.state.runtime
    assert not runtime.worker_thread.is_alive()
    assert runtime._executor._shutdown


def test_shutdown_closes_cleanly_when_the_api_never_opened_its_connection(settings, media, monotonic):
    """Stopping a service whose API thread never touched the database closes nothing and still shuts down."""
    with make_client(settings, media, monotonic) as client:
        runtime = client.app.state.runtime
    assert runtime._executor._shutdown
    assert not runtime.worker_thread.is_alive()


def test_health_is_503_when_a_thread_has_died(client):
    """A stopped worker thread is reported as not alive."""
    wait_for_staging_check(client)
    runtime = client.app.state.runtime
    runtime.worker.stop()
    runtime.worker_thread.join(10)
    response = client.get('/health')
    assert response.status_code == 503
    assert response.json()['threads'] == {'worker': False}


def test_api_stays_responsive_while_the_worker_is_stalled_on_the_share(settings, media, monotonic):
    """With the worker stuck in a marker check, as on a stalled SMB share, the API still answers (C25)."""
    entered = threading.Event()
    release = threading.Event()
    real_marker_present = staging.marker_present

    def stalled(root):
        entered.set()
        release.wait(30)
        return real_marker_present(root)

    try:
        with patch.object(staging, 'marker_present', stalled), make_client(settings, media, monotonic) as client:
            try:
                assert entered.wait(10)
                assert client.get('/jobs').status_code == 200
                assert client.post('/jobs/videos', json={'url': VIDEO_URL}).status_code == 202
                health = client.get('/health')
                assert health.status_code == 503
                assert health.json()['staging']['marker_present'] is None
            finally:
                release.set()  # before the lifespan joins the worker
    finally:
        release.set()


def test_lifespan_stops_the_worker(settings, media, monotonic):
    """Leaving the lifespan stops the worker thread (decision C36)."""
    with make_client(settings, media, monotonic) as client:
        runtime = client.app.state.runtime
        assert runtime.worker_thread.is_alive()
    assert not runtime.worker_thread.is_alive()


def test_shutdown_abandons_a_thread_that_outlasts_the_grace(settings, media, monotonic, caplog):
    """A download that outlasts the grace is abandoned, for restart recovery to requeue (decision C36)."""
    started = threading.Event()
    release = threading.Event()

    def slow_download(video, path, on_activity):
        started.set()
        release.wait(30)
        path.write_bytes(b'late')

    media.download_media = slow_download
    settings = Settings(**{**settings.__dict__, 'shutdown_grace': 0.1})
    try:
        with make_client(settings, media, monotonic) as client:
            runtime = client.app.state.runtime
            client.post('/jobs/videos', json={'url': VIDEO_URL})
            assert started.wait(10)
        assert runtime.worker_thread.is_alive()
        assert 'did not stop within the shutdown grace' in caplog.text
    finally:
        release.set()
        runtime.worker_thread.join(10)
    assert not runtime.worker_thread.is_alive()
