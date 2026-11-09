"""Tests for the YouTube worker (DESIGN 5.3, 5.4, 5.7, 5.9, 5.12-5.14); no test contacts YouTube."""

import logging
import os
import sqlite3
import threading
from dataclasses import dataclass

import pytest
import requests
import youtube_transcript_api as yta

import youtube_collector.worker as testee
from youtube_collector import staging, store
from youtube_collector.youtube import Playlist, PlaylistVideo, VideoInfo

VIDEO = 'abcdefghijk'
OTHER = 'bbbbbbbbbbb'
PLAYLIST = 'PLlist'
RETENTION = 7 * 86400.0


@dataclass(frozen=True)
class Track:
    """A listed caption track."""

    language_code: str
    is_generated: bool = False


class FakeMedia:
    """Stands in for the yt-dlp adapter, recording every call."""

    def __init__(self):
        """Start with one known video and no playlist."""
        self.titles = {VIDEO: 'A video', OTHER: 'Other video'}
        self.playlist = Playlist('A list', (PlaylistVideo(VIDEO, 'Listed A'), PlaylistVideo(OTHER, None)))
        self.expand_error = None
        self.info_error = None
        self.download_error = None
        self.during_download = None
        self.calls = []

    def expand_playlist(self, url):
        """Return the canned playlist or raise."""
        self.calls.append(('expand', url))
        if self.expand_error is not None:
            raise self.expand_error
        return self.playlist

    def video_info(self, url):
        """Return metadata for the video in the URL, or raise."""
        self.calls.append(('info', url))
        if self.info_error is not None:
            raise self.info_error
        video_id = url.rsplit('=', 1)[1]
        return VideoInfo(video_id, self.titles[video_id], {'id': video_id})

    def download_media(self, video, path, on_activity):
        """Write fake media at the stage path, reporting activity, or raise."""
        self.calls.append(('download', video.video_id, path))
        on_activity()
        if self.during_download is not None:
            self.during_download(path)
        if self.download_error is not None:
            raise self.download_error
        path.write_bytes(b'media ' + video.video_id.encode())


class FakeCaptions:
    """Stands in for the caption adapter."""

    def __init__(self):
        """List one manual English track for every video."""
        self.track = Track('en-US')
        self.find_error = None
        self.fetch_error = None
        self.calls = []

    def find_track(self, video_id, languages):
        """Return the canned track, None, or raise."""
        self.calls.append(('find', video_id, tuple(languages)))
        if self.find_error is not None:
            raise self.find_error
        return self.track

    def fetch_srt(self, track):
        """Return SRT text for the track, or raise."""
        self.calls.append(('fetch', track))
        if self.fetch_error is not None:
            raise self.fetch_error
        return f'1\n00:00:00,000 --> 00:00:01,000\n{track.language_code}\n'


class Monotonic:
    """A manually advanced monotonic clock."""

    def __init__(self):
        """Start at zero."""
        self.now = 0.0

    def __call__(self):
        """Return the current time."""
        return self.now


@pytest.fixture
def root(tmp_path):
    """Provide a staging root with its marker."""
    path = tmp_path / 'staging'
    path.mkdir()
    (path / staging.MARKER_NAME).touch()
    return path


@pytest.fixture
def db_path(tmp_path):
    """Provide an initialized database file owned by the test."""
    path = tmp_path / 'db.sqlite3'
    store.initialize(path)
    return path


@pytest.fixture
def db(db_path):
    """Provide the test thread's store."""
    with store.Store(db_path, lambda: 1_000_000.0) as opened:
        yield opened


@pytest.fixture
def media():
    """Provide the fake yt-dlp adapter."""
    return FakeMedia()


@pytest.fixture
def captions():
    """Provide the fake caption adapter."""
    return FakeCaptions()


@pytest.fixture
def clock():
    """Provide the fake monotonic clock."""
    return Monotonic()


@pytest.fixture
def worker(db_path, root, media, captions, clock):
    """Provide a worker on the test's store, staging root and fakes."""
    return testee.Worker(
        open_store=lambda: store.Store(db_path, lambda: 1_000_000.0), staging_root=root, media=media,
        captions=captions, monotonic=clock, idle_wait=60, pause_wait=60, error_wait=0,
    )


def submit(db, kind=store.JobKind.VIDEOS_WITH_CAPTIONS, video_id=VIDEO, languages=('en',)):
    """Submit a single-video job and return it."""
    return db.submit(
        kind=kind, url=f'https://youtu.be/{video_id}', target_type=store.TargetType.VIDEO, target_id=video_id,
        languages=None if kind is store.JobKind.VIDEOS else list(languages), retention_seconds=RETENTION,
    )


def submit_playlist(db, kind=store.JobKind.VIDEOS):
    """Submit a playlist job and return it."""
    return db.submit(
        kind=kind, url=f'https://www.youtube.com/playlist?list={PLAYLIST}', target_type=store.TargetType.PLAYLIST,
        target_id=PLAYLIST, languages=None if kind is store.JobKind.VIDEOS else ['en'], retention_seconds=RETENTION,
    )


def item_of(db, job, position=0):
    """Return a job's live item."""
    return db.get_job(job['id'])['items'][position]


def files(root):
    """Return every non-marker path under the staging root, relative and sorted."""
    return sorted(str(p.relative_to(root)) for p in root.rglob('*') if p.name != staging.MARKER_NAME)


def test_step_pauses_without_the_marker_and_changes_nothing(worker, db, root, media, caplog):
    """A missing marker pauses work with no record change, and the pause and resume are logged (DESIGN 5.13)."""
    job = submit(db)
    (root / staging.MARKER_NAME).unlink()
    before = db.get_job(job['id'])
    with caplog.at_level(logging.WARNING, logger=testee.__name__):
        assert worker.step(db) is testee.Step.PAUSED
        assert worker.step(db) is testee.Step.PAUSED
        assert worker.paused
        assert db.get_job(job['id']) == before
        assert media.calls == []
        (root / staging.MARKER_NAME).touch()
        assert worker.step(db) is testee.Step.PROCESSED
    assert not worker.paused
    assert [record.message.split(';')[1].strip() for record in caplog.records[:2]] == ['pausing work', 'resuming work']
    assert sum('pausing' in record.message for record in caplog.records) == 1


def test_step_is_idle_without_work(worker, db):
    """No work leaves the worker idle."""
    assert worker.step(db) is testee.Step.IDLE


def test_step_expands_a_playlist_into_items(worker, db, media):
    """Expansion persists the entries as items with listed titles (DESIGN 5.3)."""
    job = submit_playlist(db)
    assert worker.step(db) is testee.Step.EXPANDED
    expanded = db.get_job(job['id'])
    assert (expanded['state'], expanded['playlist_title']) == ('running', 'A list')
    assert [(i['video_id'], i['title']) for i in expanded['items']] == [(VIDEO, 'Listed A'), (OTHER, None)]
    assert media.calls == [('expand', f'https://www.youtube.com/playlist?list={PLAYLIST}')]


def test_step_finishes_a_playlist_whose_expansion_fails(worker, db, media):
    """A failed expansion finishes the job with a categorized failure (DESIGN 5.7, 5.9)."""
    media.expand_error = RuntimeError('ERROR: The playlist does not exist.')
    job = submit_playlist(db)
    worker.step(db)
    assert db.get_job(job['id'])['state'] == 'finished'
    [failure] = db.list_failures()
    assert (failure['phase'], failure['category'], failure['message']) == (
        'expanding', 'unavailable', 'ERROR: The playlist does not exist.',
    )


def test_videos_item_downloads_media_to_the_root(worker, db, root, media, captions):
    """A videos item downloads and publishes id - Title.mp4 at the staging root."""
    job = submit(db, kind=store.JobKind.VIDEOS)
    assert worker.step(db) is testee.Step.PROCESSED
    item = item_of(db, job)
    assert item['state'] == 'done'
    assert item['title'] == 'A video'
    assert item['media'] == {'outcome': 'downloaded', 'path': f'{VIDEO} - A video.mp4'}
    assert 'captions' not in item
    assert (root / f'{VIDEO} - A video.mp4').read_bytes() == f'media {VIDEO}'.encode()
    assert files(root) == [f'{VIDEO} - A video.mp4']
    assert db.get_job(job['id'])['state'] == 'finished'
    assert [call[0] for call in media.calls] == ['info', 'download']
    assert media.calls[0] == ('info', f'https://www.youtube.com/watch?v={VIDEO}')
    assert captions.calls == []


def test_existing_media_is_not_downloaded_again(worker, db, root, media):
    """An MP4 with the video's ID, whatever its title, counts as existing and costs no request (C23)."""
    (root / f'{VIDEO} - Old title.mp4').write_bytes(b'old')
    job = submit(db, kind=store.JobKind.VIDEOS)
    worker.step(db)
    item = item_of(db, job)
    assert item['media'] == {'outcome': 'existing', 'path': f'{VIDEO} - Old title.mp4'}
    assert media.calls == []
    assert (root / f'{VIDEO} - Old title.mp4').read_bytes() == b'old'


def test_videos_with_captions_item_writes_a_pair(worker, db, root, captions):
    """Both artifacts are written, the SRT beside its MP4, and the track is recorded (DESIGN 5.6)."""
    job = submit(db, languages=('en', 'zh'))
    worker.step(db)
    item = item_of(db, job)
    assert item['state'] == 'done'
    assert item['captions'] == {
        'outcome': 'downloaded', 'path': f'{VIDEO} - A video.srt', 'track': {'code': 'en-US', 'kind': 'manual'},
    }
    assert files(root) == [f'{VIDEO} - A video.mp4', f'{VIDEO} - A video.srt']
    assert (root / f'{VIDEO} - A video.srt').read_text(encoding='utf-8').endswith('en-US\n')
    assert captions.calls[0] == ('find', VIDEO, ('en', 'zh'))
    assert db.list_failures() == []


def test_generated_tracks_are_recorded_as_generated(worker, db, captions):
    """The track kind follows the listed track."""
    captions.track = Track('en', True)
    job = submit(db)
    worker.step(db)
    assert item_of(db, job)['captions']['track'] == {'code': 'en', 'kind': 'generated'}


def test_new_captions_keep_the_existing_mp4_stem(worker, db, root, media):
    """New captions pair with the single existing MP4, even after a title change (C33)."""
    (root / f'{VIDEO} - Old title.mp4').touch()
    job = submit(db)
    worker.step(db)
    assert item_of(db, job)['captions']['path'] == f'{VIDEO} - Old title.srt'
    assert media.calls == []


def test_captions_job_pairs_with_an_existing_mp4_without_metadata(worker, db, root, media):
    """A captions job also pairs with an existing MP4, and needs no metadata request for its name."""
    (root / f'{VIDEO} - Old title.mp4').touch()
    job = submit(db, kind=store.JobKind.CAPTIONS)
    worker.step(db)
    item = item_of(db, job)
    assert item['captions']['path'] == f'{VIDEO} - Old title.srt'
    assert 'media' not in item
    assert media.calls == []


def test_captions_job_uses_the_listed_title_without_metadata(worker, db, root, media):
    """A playlist entry's listed title names the SRT without a metadata request."""
    job = submit_playlist(db, kind=store.JobKind.CAPTIONS)
    worker.step(db)
    worker.step(db)
    assert item_of(db, job)['captions']['path'] == f'{PLAYLIST} - A list/{VIDEO} - Listed A.srt'
    assert media.calls[1:] == []


def test_captions_job_requests_metadata_when_no_title_is_known(worker, db, root, media):
    """Without a listed title or a single MP4, the title comes from a metadata request."""
    job = submit(db, kind=store.JobKind.CAPTIONS)
    worker.step(db)
    assert item_of(db, job)['captions']['path'] == f'{VIDEO} - A video.srt'
    assert [call[0] for call in media.calls] == ['info']


def test_existing_captions_are_not_fetched_again(worker, db, root, captions):
    """An existing SRT is judged on its own; the missing MP4 is still downloaded (DESIGN 5.4)."""
    (root / f'{VIDEO} - Old.srt').touch()
    job = submit(db)
    worker.step(db)
    item = item_of(db, job)
    assert item['captions'] == {'outcome': 'existing', 'path': f'{VIDEO} - Old.srt', 'track': None}
    assert item['media']['outcome'] == 'downloaded'
    assert captions.calls == []


def test_unavailable_captions_are_a_successful_outcome(worker, db, root, captions):
    """No matching track or disabled captions give unavailable, without a failure."""
    captions.track = None
    job = submit(db, kind=store.JobKind.CAPTIONS)
    worker.step(db)
    item = item_of(db, job)
    assert (item['state'], item['captions']['outcome'], item['captions']['path']) == ('done', 'unavailable', None)
    assert db.list_failures() == []
    assert files(root) == []


def test_media_failure_fails_the_item_and_skips_captions(worker, db, root, media, captions):
    """If media fails, captions are not attempted and the item fails (DESIGN 5.7)."""
    media.download_error = RuntimeError("ERROR: Sign in to confirm you're not a bot")
    job = submit(db)
    worker.step(db)
    item = item_of(db, job)
    assert item['state'] == 'failed'
    assert item['media'] == {'outcome': 'failed', 'path': None}
    assert item['captions']['outcome'] is None
    assert captions.calls == []
    [failure] = db.list_failures()
    assert (failure['phase'], failure['category'], failure['item_id']) == ('media', 'blocked', item['id'])
    assert files(root) == []


@pytest.mark.parametrize('error', [
    yta.IpBlocked(VIDEO), requests.ReadTimeout('slow'), yta.YouTubeDataUnparsable(VIDEO),
])
def test_caption_service_error_after_media_leaves_the_item_done(worker, db, root, captions, error):
    """A caption-service error after media gives error with a failure, and the item stays done (DESIGN 5.7)."""
    captions.find_error = error
    job = submit(db)
    worker.step(db)
    item = item_of(db, job)
    assert (item['state'], item['media']['outcome'], item['captions']['outcome']) == ('done', 'downloaded', 'error')
    [failure] = db.list_failures()
    assert failure['phase'] == 'captions'
    assert files(root) == [f'{VIDEO} - A video.mp4']


def test_caption_service_error_fails_a_captions_item(worker, db, captions):
    """In a captions job the captions are the purpose, so a service error fails the item."""
    captions.fetch_error = requests.ConnectionError('reset')
    job = submit(db, kind=store.JobKind.CAPTIONS)
    worker.step(db)
    item = item_of(db, job)
    assert (item['state'], item['captions']['outcome']) == ('failed', 'failed')
    [failure] = db.list_failures()
    assert (failure['phase'], failure['category']) == ('captions', 'network')


@pytest.mark.parametrize('kind', [store.JobKind.VIDEOS_WITH_CAPTIONS, store.JobKind.CAPTIONS])
def test_other_caption_errors_fail_the_item(worker, db, captions, kind):
    """A malformed caption payload fails the item in either kind (DESIGN 5.7)."""
    captions.fetch_error = ValueError('malformed caption payload')
    job = submit(db, kind=kind)
    worker.step(db)
    item = item_of(db, job)
    assert (item['state'], item['captions']['outcome']) == ('failed', 'failed')
    assert db.list_failures()[0]['message'] == 'malformed caption payload'


def test_metadata_failure_fails_the_item(worker, db, media):
    """A failed metadata request fails the item in the metadata phase."""
    media.info_error = RuntimeError('ERROR: Video unavailable')
    job = submit(db, kind=store.JobKind.VIDEOS)
    worker.step(db)
    assert item_of(db, job)['state'] == 'failed'
    [failure] = db.list_failures()
    assert (failure['phase'], failure['category']) == ('metadata', 'unavailable')


def test_a_title_that_cannot_be_named_fails_the_media(worker, db, media, root):
    """A title with nothing left to keep fails the item (DESIGN 5.4)."""
    media.titles[VIDEO] = ''
    job = submit(db, kind=store.JobKind.VIDEOS)
    worker.step(db)
    assert item_of(db, job)['media']['outcome'] == 'failed'
    assert db.list_failures()[0]['phase'] == 'media'
    assert files(root) == []


def test_unreadable_destination_fails_the_item_with_a_filesystem_failure(worker, db, root):
    """A destination that cannot be listed fails the item instead of counting as empty."""
    job = submit_playlist(db, kind=store.JobKind.CAPTIONS)
    worker.step(db)
    (root / f'{PLAYLIST} - A list').write_text('not a directory')
    worker.step(db)
    assert item_of(db, job)['state'] == 'failed'
    failure = db.list_failures()[0]
    assert (failure['phase'], failure['category']) == ('captions', 'filesystem')


def test_playlist_items_go_into_a_recreated_playlist_folder(worker, db, root):
    """Playlist items are written into playlistID - Title/, recreated if the user moved it away (DESIGN 5.4)."""
    job = submit_playlist(db)
    worker.step(db)
    worker.step(db)
    folder = root / f'{PLAYLIST} - A list'
    # Media takes the title from the metadata request, which is the current title.
    assert os.listdir(folder) == [f'{VIDEO} - A video.mp4']
    (folder / f'{VIDEO} - A video.mp4').unlink()
    folder.rmdir()
    worker.step(db)
    assert os.listdir(folder) == [f'{OTHER} - Other video.mp4']
    assert [item['media']['path'] for item in db.get_job(job['id'])['items']] == [
        f'{PLAYLIST} - A list/{VIDEO} - A video.mp4', f'{PLAYLIST} - A list/{OTHER} - Other video.mp4',
    ]


def test_failure_of_one_item_does_not_stop_the_rest(worker, db, media):
    """A failed item leaves the remaining items of its job to run (decision D17)."""
    job = submit_playlist(db)
    worker.step(db)
    media.download_error = RuntimeError('boom')
    worker.step(db)
    media.download_error = None
    worker.step(db)
    assert [item['state'] for item in db.get_job(job['id'])['items']] == ['failed', 'done']
    assert db.get_job(job['id'])['state'] == 'finished'


def test_a_later_success_supersedes_the_earlier_failure(worker, db, media):
    """A later definitive result for the same video and artifact clears the failure (DESIGN 5.9)."""
    media.download_error = RuntimeError('boom')
    submit(db, kind=store.JobKind.VIDEOS)
    worker.step(db)
    assert len(db.list_failures()) == 1
    media.download_error = None
    submit(db, kind=store.JobKind.VIDEOS)
    worker.step(db)
    assert db.list_failures() == []


def test_a_competing_file_during_download_counts_as_existing(worker, db, root, media):
    """A file that appears at the target during the download is kept and reported as existing."""
    media.during_download = lambda path: (root / f'{VIDEO} - A video.mp4').write_bytes(b'competing')
    job = submit(db, kind=store.JobKind.VIDEOS)
    worker.step(db)
    assert item_of(db, job)['media']['outcome'] == 'existing'
    assert (root / f'{VIDEO} - A video.mp4').read_bytes() == b'competing'


def test_a_marker_lost_during_download_publishes_nothing(worker, db, root, media):
    """If the share disappears mid-item, nothing is published and the item fails as filesystem (DESIGN 5.13)."""
    media.during_download = lambda path: (root / staging.MARKER_NAME).unlink()
    job = submit(db, kind=store.JobKind.VIDEOS)
    worker.step(db)
    assert item_of(db, job)['state'] == 'failed'
    assert db.list_failures()[0]['category'] == 'filesystem'
    assert files(root) == []


def test_a_marker_lost_before_captions_publishes_nothing(worker, db, root, captions):
    """The marker is checked again before the captions are published."""
    original = captions.fetch_srt

    def fetch_and_unmount(track):
        (root / staging.MARKER_NAME).unlink()
        return original(track)

    captions.fetch_srt = fetch_and_unmount
    job = submit(db, kind=store.JobKind.CAPTIONS)
    worker.step(db)
    assert item_of(db, job)['captions']['outcome'] == 'failed'
    assert db.list_failures()[0]['category'] == 'filesystem'
    assert files(root) == []


def test_a_repeat_submission_finds_the_files_and_makes_no_request(worker, db, media, captions):
    """Resubmitting after a crash creates a job that reports existing without contacting YouTube (C46)."""
    first = submit(db)
    worker.step(db)
    media.calls.clear()
    captions.calls.clear()
    second = submit(db)
    worker.step(db)
    assert second['id'] != first['id']
    item = item_of(db, second)
    assert (item['media']['outcome'], item['captions']['outcome']) == ('existing', 'existing')
    assert item['media']['path'] == item_of(db, first)['media']['path']
    assert (media.calls, captions.calls) == ([], [])


def test_staging_check_is_empty_before_the_first_check(worker):
    """Nothing has been checked before the worker's first step."""
    assert worker.staging_check() is None


def test_step_records_the_staging_check(worker, db, root, clock):
    """Each step's marker check is recorded with its time, present or not, for /health (C43)."""
    clock.now = 7.0
    worker.step(db)
    assert worker.staging_check() == testee.StagingCheck(True, 7.0)
    (root / staging.MARKER_NAME).unlink()
    clock.now = 9.0
    worker.step(db)
    assert worker.staging_check() == testee.StagingCheck(False, 9.0)


def test_a_long_download_keeps_the_staging_check_fresh_but_throttled(worker, db, media, clock):
    """Activity repeats the marker check once per interval, so a long download is not stale (C43)."""
    seen = []

    def download(video, path, on_activity):
        for now in (0.0, 10.0, 29.9, 30.0, 40.0, 60.0):
            clock.now = now
            on_activity()
            seen.append(worker.staging_check().checked_at)
        path.write_bytes(b'media')

    media.download_media = download
    submit(db, kind=store.JobKind.VIDEOS)
    worker.step(db)
    assert seen == [0.0, 0.0, 0.0, 30.0, 30.0, 60.0]


def test_a_download_reports_a_marker_lost_while_it_runs(worker, db, root, media, clock):
    """A heartbeat that finds the marker gone is visible at once; publication still refuses the file."""
    seen = []

    def download(video, path, on_activity):
        (root / staging.MARKER_NAME).unlink()
        clock.now = 30.0
        on_activity()
        seen.append(worker.staging_check().marker_present)
        path.write_bytes(b'media')

    media.download_media = download
    job = submit(db, kind=store.JobKind.VIDEOS)
    worker.step(db)
    assert seen == [False]
    assert item_of(db, job)['state'] == 'failed'
    assert files(root) == []


def test_round_robin_alternates_between_jobs(worker, db, media):
    """After a unit of one job, the next job goes next; an expansion counts as a unit (decision C9)."""
    submit_playlist(db)
    submit(db, kind=store.JobKind.VIDEOS, video_id=OTHER)
    steps = [worker.step(db) for _ in range(5)]
    downloads = [call[1] for call in media.calls if call[0] == 'download']
    assert downloads == [OTHER, VIDEO, OTHER]
    assert steps == [testee.Step.EXPANDED, *[testee.Step.PROCESSED] * 3, testee.Step.IDLE]


def test_run_recovers_processes_and_stops(worker, db, media, owned_threads):
    """The run loop recovers interrupted work, processes it, and stops after the current unit (DESIGN 5.12, C36)."""
    job = submit(db, kind=store.JobKind.VIDEOS)
    db.start_item(job['items'][0]['id'])  # interrupted by a restart
    downloaded = threading.Event()
    original = media.download_media

    def download(video, path, on_activity):
        original(video, path, on_activity)
        downloaded.set()
        worker.stop()

    media.download_media = download
    thread = owned_threads.start(worker.run, stop=worker.stop)
    assert downloaded.wait(10)
    thread.join(10)
    assert not thread.is_alive()
    assert item_of(db, job)['state'] == 'done'


def stoppable_worker(db_path, root, media, captions, clock, on_stopped, open_store=None):
    """Return a worker that reports when its run ends."""
    return testee.Worker(
        open_store=open_store or (lambda: store.Store(db_path, lambda: 1_000_000.0)), staging_root=root, media=media,
        captions=captions, monotonic=clock, idle_wait=60, pause_wait=60, error_wait=0, on_stopped=on_stopped,
    )


def test_on_stopped_runs_once_on_the_worker_thread_after_the_loop_ends(db_path, root, media, captions, clock):
    """The release happens on the thread that used the adapters, after its last step."""
    threads = []
    worker = stoppable_worker(db_path, root, media, captions, clock, lambda: threads.append(threading.current_thread()))
    worker.stop()
    worker.run()
    assert threads == [threading.current_thread()]


def test_on_stopped_runs_when_the_worker_dies_during_recovery(db_path, root, media, captions, clock):
    """A worker that never reaches its loop still releases what the adapters hold."""
    released = []

    def broken():
        raise sqlite3.OperationalError('unavailable')

    worker = stoppable_worker(db_path, root, media, captions, clock, lambda: released.append(1), open_store=broken)
    with pytest.raises(sqlite3.OperationalError):
        worker.run()
    assert released == [1]


def test_run_survives_a_failing_step(worker, caplog):
    """An unexpected error in one step is logged and the loop continues."""
    calls = []

    def step(store_):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError('database is locked')
        worker.stop()
        return testee.Step.IDLE

    worker.step = step
    with caplog.at_level(logging.ERROR, logger=testee.__name__):
        worker.run()
    assert len(calls) == 2
    assert 'Worker step failed' in caplog.text


def test_run_waits_while_paused_until_stopped(worker, root, owned_threads):
    """A paused worker waits for the pause interval, and stop ends that wait at once."""
    (root / staging.MARKER_NAME).unlink()
    steps = []
    original = worker.step

    def step(store_):
        steps.append(original(store_))
        worker.stop()  # stopping during the step, so the loop's pause wait is reached and cut short
        return steps[-1]

    worker.step = step
    thread = owned_threads.start(worker.run, stop=worker.stop)
    thread.join(10)
    assert not thread.is_alive()
    assert steps == [testee.Step.PAUSED]


def test_wake_ends_a_wait_early(worker):
    """A wake-up set before the wait ends it immediately and is consumed."""
    worker.wake()
    worker._wait(60)
    assert not worker._wakeup.is_set()
