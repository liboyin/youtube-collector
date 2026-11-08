"""Tests for the HTTP API and the service's composition (DESIGN 5.1, 5.8-5.10, 5.14, section 6).

The real lifespan runs, with the worker and reconciliation threads, against a test-owned staging
directory and database and fake adapters, so nothing reaches YouTube.
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

    def download_media(self, video, path, on_progress):
        """Write fake media, or raise the configured error."""
        on_progress(1, 2)
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
        staging_dir=staging_root, database=tmp_path / 'db.sqlite3', reconcile_interval=3600, max_wait=5,
        tombstone_retention=3600, jobs_reconcile_wait=5, staging_health_max_age=60, shutdown_grace=10,
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


def wait_until_finished(client, job):
    """Long-poll a job until it is finished, and return it."""
    view = client.get(f'/jobs/{job["id"]}').json()
    for _ in range(50):
        if view['state'] == 'finished':
            return view
        response = client.get(f'/jobs/{job["id"]}', params={'since': view['view_revision'], 'wait': 5})
        assert response.status_code == 200
        view = response.json()
    raise AssertionError(f'job did not finish: {view}')


def test_default_components_detect_cookies_once_and_build_the_real_adapters(settings):
    """The default factory builds the real adapters with the cookie detection result (DESIGN 5.11)."""
    with patch.object(testee, 'firefox_cookies_present', return_value=False) as detect, \
            patch.object(testee, 'firefox_profile_roots', return_value=()):
        components = testee.default_components(settings)
    detect.assert_called_once_with(())
    assert isinstance(components.media, testee.YoutubeClient)
    assert isinstance(components.captions, testee.CaptionClient)


def test_submit_videos_returns_202_and_the_worker_downloads(client, staging_root):
    """A submission is accepted at once and wakes the worker, which then publishes the file (DESIGN 5.1)."""
    client.get('/jobs')  # the worker has started and gone idle, waiting up to 30 s for work
    started = time.monotonic()
    response = client.post('/jobs/videos', json={'url': VIDEO_URL})
    assert response.status_code == 202
    job = response.json()
    assert (job['kind'], job['state'], job['target']) == ('videos', 'queued', {'type': 'video', 'id': VIDEO})
    finished = wait_until_finished(client, job)
    assert time.monotonic() - started < 10  # woken by the submission, not by the worker's idle timeout
    assert finished['items'][0]['media'] == {'outcome': 'downloaded', 'path': f'A video [{VIDEO}].mp4'}
    assert (staging_root / f'A video [{VIDEO}].mp4').read_bytes() == b'media'


def test_submit_videos_with_captions_writes_both(client, staging_root):
    """A combined job writes the MP4 and its SRT."""
    job = client.post('/jobs/videos-with-captions', json={'url': VIDEO_URL, 'languages': ['en']}).json()
    finished = wait_until_finished(client, job)
    assert finished['items'][0]['captions']['outcome'] == 'downloaded'
    assert (staging_root / f'A video [{VIDEO}].srt').exists()


def test_submit_captions_expands_playlists(client, staging_root):
    """A captions job on a playlist writes into its playlist folder."""
    job = client.post('/jobs/captions', json={'url': PLAYLIST_URL, 'languages': ['en-US', 'en']}).json()
    finished = wait_until_finished(client, job)
    assert [item['captions']['path'] for item in finished['items']] == [
        f'A list [PLlist]/A video [{VIDEO}].srt', f'A list [PLlist]/Other [{OTHER}].srt',
    ]


@pytest.mark.parametrize(('path', 'body', 'message'), [
    ('/jobs/videos', {'url': 'https://www.youtube.com/@someone'}, 'Channel'),
    ('/jobs/videos', {'url': 'https://www.youtube.com/playlist?list=RDabc'}, 'Mix'),
    ('/jobs/videos', {'url': VIDEO_URL, 'languages': ['en']}, 'Extra inputs'),
    ('/jobs/videos', {}, 'Field required'),
    ('/jobs/captions', {'url': VIDEO_URL}, 'Field required'),
    ('/jobs/captions', {'url': VIDEO_URL, 'languages': []}, 'at least 1'),
    ('/jobs/videos-with-captions', {'url': VIDEO_URL, 'languages': ['en', 'en_US']}, 'BCP 47'),
    ('/jobs/videos', {'url': VIDEO_URL, 'idempotency_key': ''}, 'at least 1'),
])
def test_invalid_submissions_return_422(client, path, body, message):
    """Malformed bodies, unsupported URLs, channels and bad language tags are rejected (DESIGN 5.1)."""
    response = client.post(path, json=body)
    assert response.status_code == 422
    assert message in response.text
    assert client.get('/jobs').json()['jobs'] == []


def test_idempotency_key_replays_and_conflicts(client):
    """The same key and body return the job with 200; another body is 409 (DESIGN 5.1)."""
    body = {'url': VIDEO_URL, 'languages': ['en'], 'idempotency_key': 'k1'}
    first = client.post('/jobs/captions', json=body)
    again = client.post('/jobs/captions', json=body)
    assert (first.status_code, again.status_code) == (202, 200)
    assert again.json()['id'] == first.json()['id']
    other = client.post('/jobs/captions', json={**body, 'languages': ['de']})
    assert other.status_code == 409
    endpoint = client.post('/jobs/videos-with-captions', json=body)
    assert endpoint.status_code == 409  # the kind is part of the body


def test_idempotency_key_replays_the_final_representation_after_removal(client, staging_root):
    """Once the job left the status table, its key still returns its final state with 200 (C34)."""
    body = {'url': VIDEO_URL, 'idempotency_key': 'k2'}
    job = client.post('/jobs/videos', json=body).json()
    wait_until_finished(client, job)
    (staging_root / f'A video [{VIDEO}].mp4').unlink()
    assert client.get('/jobs').json()['jobs'] == []
    replay = client.post('/jobs/videos', json=body)
    assert replay.status_code == 200
    assert (replay.json()['id'], replay.json()['removed']) == (job['id'], True)


def test_list_jobs_reconciles_and_reports_revision_and_time(client, staging_root):
    """GET /jobs requests a pass, so moved files drop out, and reports its snapshot revision (5.8, C38)."""
    job = client.post('/jobs/videos', json={'url': VIDEO_URL}).json()
    wait_until_finished(client, job)
    listed = client.get('/jobs').json()
    assert [j['id'] for j in listed['jobs']] == [job['id']]
    assert listed['reconciled_at'] is not None
    assert listed['revision'] >= listed['jobs'][0]['revision']
    (staging_root / f'A video [{VIDEO}].mp4').unlink()
    assert client.get('/jobs').json()['jobs'] == []


def test_get_job_returns_404_for_unknown_jobs(client):
    """An unknown job is 404."""
    assert client.get('/jobs/missing').status_code == 404
    assert client.get('/jobs/missing', params={'since': 0, 'wait': 0}).status_code == 404


def test_get_job_with_since_returns_when_nothing_changes_before_the_wait(client, settings):
    """A long poll on an unchanged job returns its current state after the (capped) wait."""
    job = client.post('/jobs/videos', json={'url': VIDEO_URL}).json()
    finished = wait_until_finished(client, job)
    same = client.get(f'/jobs/{job["id"]}', params={'since': finished['view_revision'], 'wait': 0})
    assert same.json()['view_revision'] == finished['view_revision']


def test_change_feed_pages_and_waits_for_changes(client):
    """The feed lists changes after the cursor and waits for the next one (DESIGN 5.10)."""
    first = client.get('/jobs/changes').json()
    assert (first['changes'], first['cursor'], first['more']) == ([], 0, False)
    job = client.post('/jobs/videos', json={'url': VIDEO_URL}).json()
    wait_until_finished(client, job)
    page = client.get('/jobs/changes', params={'since': 0, 'limit': 1}).json()
    assert (len(page['changes']), page['more']) == (1, True)
    rest = client.get('/jobs/changes', params={'since': page['cursor'], 'limit': 1000}).json()
    kinds = {(entry['kind'], entry['id']) for entry in page['changes'] + rest['changes']}
    assert ('job', job['id']) in kinds
    assert ('item', job['items'][0]['id']) in kinds
    second = client.post('/jobs/videos', json={'url': f'https://youtu.be/{OTHER}'}).json()
    waited = client.get('/jobs/changes', params={'since': rest['cursor'], 'wait': 5}).json()
    assert second['id'] in {entry['id'] for entry in waited['changes']}


def test_change_feed_caps_wait_and_validates_parameters(client):
    """The wait is capped by the settings, and out-of-range parameters are 422."""
    cursor = client.get('/jobs/changes').json()['cursor']
    assert client.get('/jobs/changes', params={'since': cursor, 'wait': 0.01}).json()['changes'] == []
    assert client.get('/jobs/changes', params={'limit': 1001}).status_code == 422
    assert client.get('/jobs/changes', params={'since': -1}).status_code == 422
    assert client.get('/jobs/changes', params={'wait': -1}).status_code == 422


def test_change_feed_and_job_waits_are_capped(settings, media, monotonic):
    """A wait above COLLECTOR_MAX_WAIT is cut to it, so idle polls cannot outlast proxies (DESIGN 5.10)."""
    settings = Settings(**{**settings.__dict__, 'max_wait': 0.2})
    with make_client(settings, media, monotonic) as client:
        job = client.post('/jobs/videos', json={'url': VIDEO_URL}).json()
        finished = wait_until_finished(client, job)
        cursor = client.get('/jobs/changes', params={'since': 0, 'limit': 1000}).json()['cursor']
        for path, params in (('/jobs/changes', {'since': cursor}), (f'/jobs/{job["id"]}',
                                                                     {'since': finished['view_revision']})):
            started = time.monotonic()
            assert client.get(path, params={**params, 'wait': 3}).status_code == 200
            assert time.monotonic() - started < 2


def test_change_feed_returns_410_below_the_pruned_revision(settings, media, monotonic, staging_root):
    """A cursor older than pruned tombstones gets 410, telling the caller to resynchronize (C27)."""
    settings = Settings(**{**settings.__dict__, 'tombstone_retention': 0})
    with make_client(settings, media, monotonic) as client:
        job = client.post('/jobs/videos', json={'url': VIDEO_URL}).json()
        wait_until_finished(client, job)
        (staging_root / f'A video [{VIDEO}].mp4').unlink()
        client.get('/jobs')  # removes the item and job, then prunes their tombstones at once
        client.get('/jobs')
        response = client.get('/jobs/changes', params={'since': 0})
        assert response.status_code == 410
        assert 'resynchronize' in response.json()['detail']


def test_failures_are_listed_and_cleared(client, media):
    """Failures appear with their reason and can be cleared one by one or all at once (DESIGN 5.9)."""
    media.download_error = RuntimeError("ERROR: Sign in to confirm you're not a bot")
    for url in (VIDEO_URL, f'https://youtu.be/{OTHER}'):
        wait_until_finished(client, client.post('/jobs/videos', json={'url': url}).json())
    listed = client.get('/failures').json()
    assert [f['category'] for f in listed['failures']] == ['blocked', 'blocked']
    assert listed['revision'] >= max(f['revision'] for f in listed['failures'])
    first = listed['failures'][0]['id']
    cleared = client.delete(f'/failures/{first}')
    assert (cleared.status_code, cleared.json()['removal']) == (200, 'cleared')
    assert client.delete(f'/failures/{first}').status_code == 404
    rest = client.delete('/failures').json()['failures']
    assert len(rest) == 1
    assert client.delete('/failures').json() == {'failures': []}
    assert client.get('/failures').json()['failures'] == []


def test_health_is_200_when_everything_is_healthy(client):
    """A healthy service reports 200 with every component and the yt-dlp version (DESIGN 5.14)."""
    client.get('/jobs')  # ensures a reconciliation pass has checked the marker
    response = client.get('/health')
    body = response.json()
    assert response.status_code == 200
    assert body['healthy'] is True
    assert body['database'] == {'ok': True, 'error': None}
    assert (body['staging']['ok'], body['staging']['marker_present'], body['staging']['paused']) == (True, True, False)
    assert body['threads'] == {'worker': True, 'reconciler': True}
    assert body['yt_dlp_version'] == testee.yt_dlp.version.__version__


def test_health_is_503_while_the_marker_is_missing(client, staging_root):
    """A missing marker makes the service unhealthy and reports work as paused (5.13, 5.14)."""
    (staging_root / staging.MARKER_NAME).unlink()
    client.get('/jobs')
    response = client.get('/health')
    assert response.status_code == 503
    assert response.json()['staging'] == {'ok': False, 'marker_present': False,
                                          'checked_seconds_ago': 0.0, 'paused': True}


def test_health_is_503_when_the_staging_check_is_stale(client, monotonic):
    """A cached staging check older than the limit counts as unhealthy (C35)."""
    client.get('/jobs')
    monotonic.now += 61
    response = client.get('/health')
    assert response.status_code == 503
    assert response.json()['staging']['checked_seconds_ago'] == 61.0


def test_health_is_503_when_the_database_fails(client):
    """A database that cannot be locked for writing is unhealthy."""
    client.get('/jobs')
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
                client.get('/jobs/changes')
            response = client.get('/health')
            assert response.status_code == 503
            assert response.json()['database'] == {'ok': False, 'error': 'unable to open database file'}
            failing.clear()
            client.get('/jobs')
            assert client.get('/health').status_code == 200
        runtime = client.app.state.runtime
    assert not runtime.worker_thread.is_alive()
    assert runtime._executor._shutdown


def test_shutdown_closes_cleanly_when_the_api_never_opened_its_connection(settings, media, monotonic):
    """Stopping a service whose API thread never touched the database closes nothing and still shuts down."""
    with make_client(settings, media, monotonic) as client:
        runtime = client.app.state.runtime
    assert runtime._executor._shutdown
    assert not runtime.reconcile_thread.is_alive()


def test_health_is_503_when_a_thread_has_died(client):
    """A stopped worker thread is reported as not alive."""
    client.get('/jobs')
    runtime = client.app.state.runtime
    runtime.worker.stop()
    runtime.worker_thread.join(10)
    response = client.get('/health')
    assert response.status_code == 503
    assert response.json()['threads'] == {'worker': False, 'reconciler': True}


def test_api_stays_responsive_while_a_staging_probe_is_stalled(settings, media, monotonic):
    """With the reconciler stuck on the share, GET /jobs returns after its bounded wait (C25)."""
    entered = threading.Event()
    release = threading.Event()

    class StalledReconciler(testee.Reconciler):
        """A reconciler whose staging probe blocks until released, as on a stalled SMB share."""

        def _reconcile(self, store):
            entered.set()
            release.wait(30)
            return super()._reconcile(store)

    settings = Settings(**{**settings.__dict__, 'jobs_reconcile_wait': 0.1})
    with patch.object(testee, 'Reconciler', StalledReconciler):
        client = make_client(settings, media, monotonic)
        try:
            with client:
                assert entered.wait(10)
                listed = client.get('/jobs')
                assert (listed.status_code, listed.json()['reconciled_at']) == (200, None)
                health = client.get('/health')
                assert health.status_code == 503
                assert health.json()['staging']['marker_present'] is None
                release.set()
        finally:
            release.set()


def test_lifespan_stops_both_threads(settings, media, monotonic):
    """Leaving the lifespan stops the worker and reconciliation threads (decision C36)."""
    with make_client(settings, media, monotonic) as client:
        runtime = client.app.state.runtime
        assert runtime.worker_thread.is_alive()
    assert not runtime.worker_thread.is_alive()
    assert not runtime.reconcile_thread.is_alive()


def test_shutdown_abandons_a_thread_that_outlasts_the_grace(settings, media, monotonic, caplog):
    """A download that outlasts the grace is abandoned, for restart recovery to requeue (decision C36)."""
    started = threading.Event()
    release = threading.Event()

    def slow_download(video, path, on_progress):
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
