"""Tests for the SQLite store (DESIGN 5.1, 5.3, 5.7-5.10, 5.12 and section 7)."""

import sqlite3
from contextlib import closing
from unittest.mock import patch

import pytest

import youtube_collector.store as testee
from youtube_collector.categories import Category

VIDEO = 'dQw4w9WgXcQ'
OTHER_VIDEO = 'aaaaaaaaaaa'
PLAYLIST = 'PLabc'
RETENTION = 100.0


class Clock:
    """A manually advanced clock."""

    def __init__(self, now=1_000_000.0):
        """Start at a fixed epoch time."""
        self.now = now

    def __call__(self):
        """Return the current time."""
        return self.now

    def advance(self, seconds):
        """Move the clock forward."""
        self.now += seconds


@pytest.fixture
def clock():
    """Provide a test-owned clock."""
    return Clock()


@pytest.fixture
def db_path(tmp_path):
    """Provide an initialized database file owned by the test."""
    path = tmp_path / 'collector.sqlite3'
    testee.initialize(path)
    return path


@pytest.fixture
def store(db_path, clock):
    """Provide a store on the test's database, closed afterwards."""
    with testee.Store(db_path, clock) as opened:
        yield opened


def submit_video(store, kind=testee.JobKind.VIDEOS_WITH_CAPTIONS, video_id=VIDEO, retention=RETENTION):
    """Submit a single-video job."""
    languages = None if kind is testee.JobKind.VIDEOS else ['en']
    return store.submit(
        kind=kind, url=f'https://youtu.be/{video_id}', target_type=testee.TargetType.VIDEO, target_id=video_id,
        languages=languages, retention_seconds=retention,
    )


def submit_playlist(store, kind=testee.JobKind.VIDEOS, playlist_id=PLAYLIST, retention=RETENTION):
    """Submit a playlist job."""
    languages = None if kind is testee.JobKind.VIDEOS else ['en']
    return store.submit(
        kind=kind, url=f'https://www.youtube.com/playlist?list={playlist_id}', target_type=testee.TargetType.PLAYLIST,
        target_id=playlist_id, languages=languages, retention_seconds=retention,
    )


def expand(store, job_id, video_ids, title='Playlist'):
    """Expand a playlist job into items for the given videos."""
    store.start_expansion(job_id)
    store.complete_expansion(job_id, title, [testee.PlaylistEntry(video_id, f'T {video_id}') for video_id in video_ids])


def media_done(path='a [x].mp4', outcome=testee.MediaOutcome.DOWNLOADED, failures=()):
    """Return a done result with media only."""
    return testee.ItemResult(state=testee.ItemState.DONE, media_outcome=outcome, media_path=path, failures=failures)


def failure(phase=testee.Phase.MEDIA, category=Category.NETWORK, message='boom'):
    """Return a new failure."""
    return testee.NewFailure(phase, category, message)


def job_item(store, job_id, position=0):
    """Return a job's live item at a position."""
    return store.get_job(job_id)['items'][position]


def finished_job(store, kind=testee.JobKind.VIDEOS, video_id=VIDEO):
    """Submit a single-video job and finish it, returning the job as submitted."""
    job = submit_video(store, kind=kind, video_id=video_id)
    store.finish_item(job['items'][0]['id'], media_done())
    return job


def count_rows(db_path, table):
    """Return how many rows a table holds."""
    with closing(sqlite3.connect(db_path)) as connection:
        return connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]


def test_job_kind_reports_requested_artifacts():
    """Each kind requests media, captions or both."""
    assert [(k.wants_media, k.wants_captions) for k in testee.JobKind] == [(True, False), (False, True), (True, True)]


def test_initialize_creates_the_schema_in_wal_mode(db_path):
    """The schema is created with the current version and WAL journaling."""
    with closing(sqlite3.connect(db_path)) as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0] == testee.SCHEMA_VERSION
        assert connection.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'


def test_initialize_keeps_existing_data(db_path, clock):
    """Initializing an existing database again changes nothing."""
    with testee.Store(db_path, clock) as store:
        job = submit_video(store)
    testee.initialize(db_path)
    with testee.Store(db_path, clock) as store:
        assert store.get_job(job['id']) == job


def test_initialize_rolls_back_a_failed_schema_creation(tmp_path):
    """A failure while creating the schema leaves the file without a version, so a retry starts over."""
    path = tmp_path / 'broken.sqlite3'
    with patch.object(testee, '_SCHEMA', testee._SCHEMA + 'CREATE TABLE jobs (x);'), \
            pytest.raises(sqlite3.OperationalError):
        testee.initialize(path)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 0
        assert connection.execute("SELECT name FROM sqlite_master WHERE name = 'jobs'").fetchone() is None


@pytest.mark.parametrize('version', [1, 99])
def test_initialize_rejects_another_schema_version(tmp_path, version):
    """A database from another schema version is refused with its version: there is no migration (C44)."""
    path = tmp_path / 'other.sqlite3'
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(f'PRAGMA user_version = {version}')
    with pytest.raises(RuntimeError, match=str(version)):
        testee.initialize(path)


def test_iso_formats_utc_timestamps():
    """Timestamps are ISO 8601 UTC, and None stays None."""
    assert testee._iso(0.0) == '1970-01-01T00:00:00+00:00'
    assert testee._iso(None) is None


def test_store_rejects_an_uninitialized_database(tmp_path, clock):
    """Opening a store on a file without the schema fails."""
    with pytest.raises(RuntimeError, match='schema version 0'):
        testee.Store(tmp_path / 'empty.sqlite3', clock)


def test_store_refuses_use_from_another_thread(store, owned_threads):
    """A connection belongs to the thread that opened it (decision C36)."""
    errors = []

    def use():
        try:
            store.list_jobs()
        except sqlite3.ProgrammingError as error:
            errors.append(error)

    owned_threads.start(use).join(10)
    assert len(errors) == 1


def test_store_closes_on_exit(db_path, clock):
    """Leaving the context closes the connection."""
    with testee.Store(db_path, clock) as store:
        pass
    with pytest.raises(sqlite3.ProgrammingError):
        store.list_jobs()


def test_write_rolls_back_every_statement_when_one_fails(store):
    """A write that fails part-way leaves no partial state."""
    job = submit_playlist(store)
    store.start_expansion(job['id'])
    entry = testee.PlaylistEntry(VIDEO)
    with pytest.raises(sqlite3.IntegrityError), store._write() as connection:
        store._insert_item(connection, job['id'], 0, entry, 0.0)
        store._insert_item(connection, job['id'], 0, entry, 0.0)  # same position
    assert store.get_job(job['id'])['items'] == []


def test_check_passes_on_a_writable_database_and_changes_nothing(store):
    """The health check succeeds and writes nothing."""
    job = submit_video(store)
    store.check()
    assert store.list_jobs() == [job]


def test_check_fails_while_another_connection_holds_the_write_lock(db_path, clock):
    """A database that cannot be locked for writing fails the check."""
    other = sqlite3.connect(db_path, timeout=0, autocommit=True)
    try:
        other.execute('BEGIN IMMEDIATE')
        with testee.Store(db_path, clock) as store:
            store._connection.execute('PRAGMA busy_timeout = 0')
            with pytest.raises(sqlite3.OperationalError):
                store.check()
    finally:
        other.close()


def test_submit_creates_a_playlist_job_without_items(store):
    """A playlist job has no items until expansion."""
    job = submit_playlist(store)
    assert job['items'] == []
    assert job['target'] == {'type': 'playlist', 'id': PLAYLIST}
    assert job['languages'] is None


def test_submit_creates_a_video_job_with_its_item(store, clock):
    """A single-video job gets its one queued item at submission, with exactly the documented fields."""
    job = submit_video(store)
    assert set(job) == {'id', 'kind', 'url', 'target', 'languages', 'state', 'playlist_title', 'created_at',
                        'updated_at', 'items', 'failures'}
    assert job['kind'] == 'videos-with-captions'
    assert job['target'] == {'type': 'video', 'id': VIDEO}
    assert job['languages'] == ['en']
    assert job['state'] == 'queued'
    assert job['created_at'] == testee._iso(clock.now)
    assert job['failures'] == []
    [item] = job['items']
    assert set(item) == {'id', 'job_id', 'position', 'video_id', 'title', 'state', 'media', 'captions', 'created_at',
                         'updated_at'}
    assert (item['video_id'], item['state'], item['position'], item['title']) == (VIDEO, 'queued', 0, None)
    assert item['media'] == {'outcome': None, 'path': None}
    assert item['captions'] == {'outcome': None, 'path': None, 'track': None}


def test_submit_always_creates_a_new_job(store):
    """The same submission twice is two jobs; there are no idempotency keys (C46)."""
    assert submit_video(store)['id'] != submit_video(store)['id']
    assert len(store.list_jobs()) == 2


def test_submit_deletes_a_finished_job_at_its_retention_with_its_items(store, clock, db_path):
    """A finished job goes with its items once the retention has passed, and not a moment before (C42)."""
    job = finished_job(store)
    clock.advance(RETENTION - 1)
    submit_playlist(store, playlist_id='PLa')
    assert store.get_job(job['id']) is not None
    assert count_rows(db_path, 'items') == 1
    clock.advance(1)
    submit_playlist(store, playlist_id='PLb')
    assert store.get_job(job['id']) is None
    assert count_rows(db_path, 'items') == 0


@pytest.mark.parametrize('finish', ['item', 'empty expansion', 'failed expansion'])
def test_every_way_of_finishing_starts_the_retention(store, clock, finish):
    """A job finished by its last item, an empty playlist or a failed expansion all expire."""
    if finish == 'item':
        job = finished_job(store)
    else:
        job = submit_playlist(store)
        if finish == 'empty expansion':
            expand(store, job['id'], [])
        else:
            store.fail_expansion(job['id'], failure(testee.Phase.EXPANDING))
    clock.advance(RETENTION)
    submit_playlist(store, playlist_id='PLnew')
    assert store.get_job(job['id']) is None


def test_retention_counts_from_when_the_job_finished(store, clock):
    """An old job that finished recently is kept: the retention starts at the finish, not the submission."""
    job = submit_video(store, kind=testee.JobKind.VIDEOS)
    clock.advance(10 * RETENTION)
    store.finish_item(job['items'][0]['id'], media_done())
    clock.advance(RETENTION - 1)
    submit_playlist(store, playlist_id='PLnew')
    assert store.get_job(job['id']) is not None


def test_submit_never_deletes_an_unfinished_job(store, clock):
    """Queued, expanding and running jobs stay, however old they are (DESIGN 5.8)."""
    queued = submit_video(store)
    expanding = submit_playlist(store, playlist_id='PLe')
    store.start_expansion(expanding['id'])
    running = submit_playlist(store, playlist_id='PLr')
    expand(store, running['id'], [VIDEO, OTHER_VIDEO])
    first = job_item(store, running['id'], 0)['id']
    store.finish_item(first, media_done())
    clock.advance(1000 * RETENTION)
    submit_playlist(store, playlist_id='PLnew', retention=0)
    for job in (queued, expanding, running):
        assert store.get_job(job['id']) is not None
    assert len(store.get_job(running['id'])['items']) == 2


def test_failures_outlive_their_expired_job(store, clock):
    """A failure stays visible after its job is gone, until it is cleared or superseded (DESIGN 5.8)."""
    job = submit_playlist(store)
    store.fail_expansion(job['id'], failure(testee.Phase.EXPANDING, message='gone'))
    clock.advance(RETENTION)
    submit_video(store)
    assert store.get_job(job['id']) is None
    [record] = store.list_failures()
    assert (record['job_id'], record['message']) == (job['id'], 'gone')
    assert store.clear_failure(record['id'])['message'] == 'gone'


def test_a_failure_of_an_expired_job_can_still_be_superseded(store, clock):
    """Supersession does not need the failure's job to exist."""
    old = submit_video(store, kind=testee.JobKind.VIDEOS)
    store.finish_item(old['items'][0]['id'], testee.ItemResult(state=testee.ItemState.FAILED, failures=[failure()]))
    clock.advance(RETENTION)
    retry = submit_video(store, kind=testee.JobKind.VIDEOS)
    assert store.get_job(old['id']) is None
    store.finish_item(retry['items'][0]['id'], media_done())
    assert store.list_failures() == []


def test_reads_do_not_delete_expired_jobs(store, clock):
    """Without a new submission an expired job stays readable: the retention is a lower bound (C42)."""
    job = finished_job(store)
    clock.advance(1000 * RETENTION)
    assert store.get_job(job['id']) is not None
    assert [listed['id'] for listed in store.list_jobs()] == [job['id']]


def test_get_job_returns_none_for_an_unknown_job(store):
    """An unknown job is absent."""
    assert store.get_job('missing') is None


def test_list_jobs_returns_jobs_in_submission_order_with_their_items(store):
    """The status table lists every retained job in order, finished ones included."""
    first = submit_playlist(store)
    second = submit_video(store)
    expand(store, first['id'], [VIDEO, OTHER_VIDEO])
    item = job_item(store, first['id'], 0)
    store.finish_item(item['id'], media_done())
    jobs = store.list_jobs()
    assert [job['id'] for job in jobs] == [first['id'], second['id']]
    assert [item['video_id'] for item in jobs[0]['items']] == [VIDEO, OTHER_VIDEO]


def test_a_job_embeds_only_its_own_uncleared_failures_oldest_first(store):
    """One read tells a caller how a job ended (C39); other jobs' failures stay out of it."""
    job = submit_playlist(store)
    expand(store, job['id'], [VIDEO, OTHER_VIDEO])
    first, second = store.get_job(job['id'])['items']
    store.finish_item(first['id'], testee.ItemResult(state=testee.ItemState.FAILED, failures=[failure(message='a')]))
    store.finish_item(second['id'], testee.ItemResult(state=testee.ItemState.FAILED, failures=[failure(message='b')]))
    other = submit_playlist(store, playlist_id='PLo')
    store.fail_expansion(other['id'], failure(testee.Phase.EXPANDING, message='other'))
    assert [f['message'] for f in store.get_job(job['id'])['failures']] == ['a', 'b']
    assert store.get_job(job['id'])['failures'] == store.list_failures()[:2]


def test_a_cleared_failure_disappears_from_its_job(store):
    """Clearing removes the failure from the job's representation; the item's outcome stays."""
    job = submit_video(store, kind=testee.JobKind.VIDEOS)
    item_id = job['items'][0]['id']
    store.finish_item(item_id, testee.ItemResult(
        state=testee.ItemState.FAILED, media_outcome=testee.MediaOutcome.FAILED, failures=[failure()],
    ))
    [record] = store.get_job(job['id'])['failures']
    store.clear_failure(record['id'])
    cleared = store.get_job(job['id'])
    assert cleared['failures'] == []
    assert cleared['items'][0]['media']['outcome'] == 'failed'


def test_a_superseded_failure_disappears_from_its_earlier_job(store):
    """A later success for the same video removes the failure from the earlier job too (DESIGN 5.9)."""
    old = submit_video(store, kind=testee.JobKind.VIDEOS)
    store.finish_item(old['items'][0]['id'], testee.ItemResult(state=testee.ItemState.FAILED, failures=[failure()]))
    retry = submit_video(store, kind=testee.JobKind.VIDEOS)
    store.finish_item(retry['items'][0]['id'], media_done())
    assert store.get_job(old['id'])['failures'] == []


def test_list_failures_returns_uncleared_failures_oldest_first(store):
    """Failures are listed in creation order, and cleared ones disappear."""
    first = submit_playlist(store, playlist_id='PLa')
    second = submit_playlist(store, playlist_id='PLb')
    store.fail_expansion(first['id'], failure(testee.Phase.EXPANDING, message='a'))
    store.fail_expansion(second['id'], failure(testee.Phase.EXPANDING, message='b'))
    failures = store.list_failures()
    assert [f['message'] for f in failures] == ['a', 'b']
    store.clear_failure(failures[0]['id'])
    assert [f['message'] for f in store.list_failures()] == ['b']


def test_next_work_returns_nothing_without_work(store):
    """An empty store has no work."""
    assert store.next_work(0) is None


def test_next_work_expands_a_queued_playlist_first(store):
    """A queued playlist job's unit is its expansion."""
    job = submit_playlist(store, kind=testee.JobKind.CAPTIONS)
    unit = store.next_work(0)
    assert unit.job_id == job['id']
    assert unit.item is None
    assert unit.kind is testee.JobKind.CAPTIONS
    assert unit.target_type is testee.TargetType.PLAYLIST
    assert unit.languages == ('en',)
    store.start_expansion(job['id'])
    assert store.next_work(0) is None


def test_next_work_takes_items_in_playlist_order(store):
    """Within a job, the first queued item comes first."""
    job = submit_playlist(store)
    expand(store, job['id'], [VIDEO, OTHER_VIDEO], title='List')
    unit = store.next_work(0)
    assert unit.item == testee.WorkItem(job_item(store, job['id'], 0)['id'], VIDEO, f'T {VIDEO}')
    assert unit.playlist_title == 'List'
    store.start_item(unit.item.id)
    assert store.next_work(unit.job_seq).item.video_id == OTHER_VIDEO


def test_next_work_rotates_round_robin_across_jobs(store):
    """After a unit of one job, the next job in submission order goes next, wrapping around (C9)."""
    playlist = submit_playlist(store)
    expand(store, playlist['id'], ['v' * 11, 'w' * 11, 'x' * 11])
    single = submit_video(store)
    order = []
    after = 0
    while (unit := store.next_work(after)) is not None:
        order.append(unit.item.video_id)
        store.start_item(unit.item.id)
        store.finish_item(unit.item.id, media_done())
        after = unit.job_seq
    assert order == ['v' * 11, VIDEO, 'w' * 11, 'x' * 11]
    assert store.get_job(single['id'])['state'] == 'finished'


def test_next_work_skips_finished_jobs(store):
    """Finished jobs have no work."""
    job = submit_playlist(store)
    store.fail_expansion(job['id'], failure(testee.Phase.EXPANDING))
    assert store.next_work(0) is None


def test_start_expansion_marks_the_job_expanding(store):
    """Expansion moves the job to expanding."""
    job = submit_playlist(store)
    store.start_expansion(job['id'])
    assert store.get_job(job['id'])['state'] == 'expanding'


def test_complete_expansion_persists_items_and_starts_the_job(store):
    """Entries become queued items in playlist order and the job runs (DESIGN 5.3)."""
    job = submit_playlist(store, kind=testee.JobKind.CAPTIONS)
    expand(store, job['id'], [VIDEO, OTHER_VIDEO], title='List')
    expanded = store.get_job(job['id'])
    assert expanded['state'] == 'running'
    assert expanded['playlist_title'] == 'List'
    assert [(i['position'], i['video_id'], i['title']) for i in expanded['items']] == [
        (0, VIDEO, f'T {VIDEO}'), (1, OTHER_VIDEO, f'T {OTHER_VIDEO}'),
    ]
    assert 'media' not in expanded['items'][0]
    assert 'captions' in expanded['items'][0]


def test_complete_expansion_without_entries_finishes_the_job(store):
    """An empty playlist finishes at once."""
    job = submit_playlist(store)
    expand(store, job['id'], [])
    assert store.get_job(job['id'])['state'] == 'finished'


def test_complete_expansion_supersedes_failures_of_the_same_playlist_only(store):
    """A successful expansion clears earlier expansion failures for that playlist (DESIGN 5.9)."""
    failed = submit_playlist(store)
    other = submit_playlist(store, playlist_id='PLother')
    store.fail_expansion(failed['id'], failure(testee.Phase.EXPANDING))
    store.fail_expansion(other['id'], failure(testee.Phase.EXPANDING))
    retry = submit_playlist(store)
    expand(store, retry['id'], [VIDEO])
    [remaining] = store.list_failures()
    assert remaining['playlist_id'] == 'PLother'


def test_fail_expansion_finishes_the_job_with_a_failure(store):
    """A failed expansion finishes the job with no items and records the failure (DESIGN 5.7)."""
    job = submit_playlist(store, kind=testee.JobKind.CAPTIONS)
    store.start_expansion(job['id'])
    store.fail_expansion(job['id'], failure(testee.Phase.EXPANDING, Category.UNAVAILABLE, 'gone'))
    assert store.get_job(job['id'])['state'] == 'finished'
    [record] = store.list_failures()
    assert {key: record[key] for key in ('job_id', 'job_kind', 'item_id', 'video_id', 'playlist_id', 'phase',
                                          'category', 'message')} == {
        'job_id': job['id'], 'job_kind': 'captions', 'item_id': None, 'video_id': None, 'playlist_id': PLAYLIST,
        'phase': 'expanding', 'category': 'unavailable', 'message': 'gone',
    }
    assert record['url'] == job['url']


def test_start_item_runs_the_item_and_its_queued_job(store):
    """Starting an item marks it and a queued job running."""
    job = submit_video(store)
    store.start_item(job['items'][0]['id'])
    started = store.get_job(job['id'])
    assert (started['state'], started['items'][0]['state']) == ('running', 'running')


def test_start_item_leaves_a_running_job_alone(store, clock):
    """Starting a later item does not touch a job that is already running."""
    job = submit_playlist(store)
    expand(store, job['id'], [VIDEO, OTHER_VIDEO])
    updated_at = store.get_job(job['id'])['updated_at']
    clock.advance(5)
    store.start_item(job_item(store, job['id'], 0)['id'])
    assert store.get_job(job['id'])['updated_at'] == updated_at


def test_finish_item_records_outcomes_and_finishes_the_job(store):
    """A finished item keeps its outcomes, paths and track, and ends its job."""
    job = submit_video(store)
    item_id = job['items'][0]['id']
    store.start_item(item_id)
    store.finish_item(item_id, testee.ItemResult(
        state=testee.ItemState.DONE, title='Title', media_outcome=testee.MediaOutcome.DOWNLOADED,
        media_path='Title [x].mp4', captions_outcome=testee.CaptionsOutcome.DOWNLOADED,
        captions_path='Title [x].srt', track_code='en-US', track_kind=testee.TrackKind.GENERATED,
    ))
    finished = store.get_job(job['id'])
    item = finished['items'][0]
    assert finished['state'] == 'finished'
    assert (item['state'], item['title']) == ('done', 'Title')
    assert item['media'] == {'outcome': 'downloaded', 'path': 'Title [x].mp4'}
    assert item['captions'] == {'outcome': 'downloaded', 'path': 'Title [x].srt',
                                'track': {'code': 'en-US', 'kind': 'generated'}}


def test_finish_item_keeps_the_listed_title_when_none_is_given(store):
    """A result without a title keeps the title the playlist listing supplied."""
    job = submit_playlist(store)
    expand(store, job['id'], [VIDEO])
    item_id = job_item(store, job['id'])['id']
    store.finish_item(item_id, media_done())
    assert job_item(store, job['id'])['title'] == f'T {VIDEO}'


def test_finish_item_keeps_the_job_running_while_items_remain(store):
    """A job finishes only when no item is queued or running."""
    job = submit_playlist(store)
    expand(store, job['id'], [VIDEO, OTHER_VIDEO])
    first, second = store.get_job(job['id'])['items']
    store.finish_item(first['id'], testee.ItemResult(state=testee.ItemState.FAILED, failures=[failure()]))
    assert store.get_job(job['id'])['state'] == 'running'
    store.start_item(second['id'])
    store.finish_item(second['id'], media_done())
    assert store.get_job(job['id'])['state'] == 'finished'


def test_finish_item_records_failures_with_their_context(store):
    """An item's failures carry its job, item, video and playlist context (DESIGN 5.9)."""
    job = submit_playlist(store, kind=testee.JobKind.VIDEOS_WITH_CAPTIONS)
    expand(store, job['id'], [VIDEO], title='List')
    item_id = job_item(store, job['id'])['id']
    store.finish_item(item_id, testee.ItemResult(
        state=testee.ItemState.DONE, media_outcome=testee.MediaOutcome.DOWNLOADED, media_path='a.mp4',
        captions_outcome=testee.CaptionsOutcome.ERROR,
        failures=[failure(testee.Phase.CAPTIONS, Category.NETWORK, 'timeout')],
    ))
    [record] = store.list_failures()
    assert (record['item_id'], record['video_id'], record['playlist_id'], record['playlist_title']) == (
        item_id, VIDEO, PLAYLIST, 'List',
    )
    assert (record['phase'], record['category'], record['job_kind']) == ('captions', 'network', 'videos-with-captions')


def test_finish_item_rejects_non_final_states(store):
    """Only done and failed are final."""
    item_id = submit_video(store)['items'][0]['id']
    with pytest.raises(ValueError):
        store.finish_item(item_id, testee.ItemResult(state=testee.ItemState.RUNNING))


@pytest.mark.parametrize('outcome', [testee.MediaOutcome.DOWNLOADED, testee.MediaOutcome.EXISTING])
def test_finish_item_supersedes_media_failures_of_the_same_video(store, outcome):
    """A definitive media outcome clears earlier media and media-metadata failures for that video (DESIGN 5.9)."""
    for phase in (testee.Phase.MEDIA, testee.Phase.METADATA):
        old = submit_video(store, kind=testee.JobKind.VIDEOS)['items'][0]['id']
        store.finish_item(old, testee.ItemResult(state=testee.ItemState.FAILED, failures=[failure(phase)]))
    other = submit_video(store, kind=testee.JobKind.VIDEOS, video_id=OTHER_VIDEO)['items'][0]['id']
    store.finish_item(other, testee.ItemResult(state=testee.ItemState.FAILED, failures=[failure()]))
    retry = submit_video(store, kind=testee.JobKind.VIDEOS)['items'][0]['id']
    store.finish_item(retry, media_done(outcome=outcome))
    assert [f['video_id'] for f in store.list_failures()] == [OTHER_VIDEO]


def test_finish_item_does_not_supersede_on_a_failed_outcome(store):
    """A failed media outcome is not definitive and clears nothing."""
    old = submit_video(store, kind=testee.JobKind.VIDEOS)['items'][0]['id']
    store.finish_item(old, testee.ItemResult(state=testee.ItemState.FAILED, failures=[failure()]))
    retry = submit_video(store, kind=testee.JobKind.VIDEOS)['items'][0]['id']
    store.finish_item(retry, testee.ItemResult(state=testee.ItemState.FAILED, media_outcome=testee.MediaOutcome.FAILED,
                                               failures=[failure()]))
    assert len(store.list_failures()) == 2


@pytest.mark.parametrize('outcome', [
    testee.CaptionsOutcome.DOWNLOADED, testee.CaptionsOutcome.EXISTING, testee.CaptionsOutcome.UNAVAILABLE,
])
def test_finish_item_supersedes_caption_failures_but_not_media_ones(store, outcome):
    """A definitive captions outcome clears caption failures only, keeping the video's media failures."""
    old = submit_video(store)['items'][0]['id']
    store.finish_item(old, testee.ItemResult(state=testee.ItemState.FAILED, media_outcome=testee.MediaOutcome.FAILED,
                                             failures=[failure(testee.Phase.MEDIA)]))
    caption_old = submit_video(store, kind=testee.JobKind.CAPTIONS)['items'][0]['id']
    store.finish_item(caption_old, testee.ItemResult(state=testee.ItemState.FAILED,
                                                     failures=[failure(testee.Phase.CAPTIONS)]))
    caption_metadata = submit_video(store, kind=testee.JobKind.CAPTIONS)['items'][0]['id']
    store.finish_item(caption_metadata, testee.ItemResult(state=testee.ItemState.FAILED,
                                                          failures=[failure(testee.Phase.METADATA)]))
    retry = submit_video(store, kind=testee.JobKind.CAPTIONS)['items'][0]['id']
    store.finish_item(retry, testee.ItemResult(state=testee.ItemState.DONE, captions_outcome=outcome))
    assert [f['phase'] for f in store.list_failures()] == ['media']


def test_finish_item_does_not_supersede_on_caption_errors(store):
    """A caption error is not definitive, so earlier caption failures stay."""
    old = submit_video(store, kind=testee.JobKind.CAPTIONS)['items'][0]['id']
    store.finish_item(old, testee.ItemResult(state=testee.ItemState.FAILED, failures=[failure(testee.Phase.CAPTIONS)]))
    retry = submit_video(store)['items'][0]['id']
    store.finish_item(retry, testee.ItemResult(
        state=testee.ItemState.DONE, media_outcome=testee.MediaOutcome.DOWNLOADED, media_path='a.mp4',
        captions_outcome=testee.CaptionsOutcome.ERROR, failures=[failure(testee.Phase.CAPTIONS)],
    ))
    assert [f['item_id'] for f in store.list_failures()] == [old, retry]


def test_clear_failures_returns_the_cleared_records_oldest_first(store):
    """Clearing everything returns every record once, in creation order."""
    for playlist_id, message in (('PLa', 'a'), ('PLb', 'b')):
        job = submit_playlist(store, playlist_id=playlist_id)
        store.fail_expansion(job['id'], failure(testee.Phase.EXPANDING, message=message))
    assert [record['message'] for record in store.clear_failures()] == ['a', 'b']
    assert store.list_failures() == []
    assert store.clear_failures() == []


def test_clear_failure_clears_one_failure(store):
    """Clearing one failure returns it once, then reports it unknown, leaving the others."""
    for playlist_id in ('PLa', 'PLb'):
        store.fail_expansion(submit_playlist(store, playlist_id=playlist_id)['id'], failure(testee.Phase.EXPANDING))
    first, second = store.list_failures()
    assert store.clear_failure(first['id']) == first
    assert store.clear_failure(first['id']) is None
    assert store.clear_failure('missing') is None
    assert store.list_failures() == [second]


def test_recover_requeues_running_items_and_expanding_jobs(store):
    """After a restart, running items and expanding jobs start over (DESIGN 5.12)."""
    video = submit_video(store)
    item_id = video['items'][0]['id']
    store.start_item(item_id)
    playlist = submit_playlist(store)
    store.start_expansion(playlist['id'])
    store.recover()
    assert store.get_job(video['id'])['items'][0]['state'] == 'queued'
    assert store.get_job(playlist['id'])['state'] == 'queued'
    assert store.next_work(0).item.id == item_id


def test_update_rejects_unknown_rows(store):
    """Writing to a row that does not exist is an error."""
    with pytest.raises(LookupError):
        store.start_expansion('missing')
    assert store.list_jobs() == []


@pytest.mark.parametrize(('phase', 'kind', 'artifact'), [
    (testee.Phase.EXPANDING, testee.JobKind.VIDEOS, 'playlist'),
    (testee.Phase.MEDIA, testee.JobKind.VIDEOS, 'media'),
    (testee.Phase.CAPTIONS, testee.JobKind.CAPTIONS, 'captions'),
    (testee.Phase.METADATA, testee.JobKind.VIDEOS, 'media'),
    (testee.Phase.METADATA, testee.JobKind.VIDEOS_WITH_CAPTIONS, 'media'),
    (testee.Phase.METADATA, testee.JobKind.CAPTIONS, 'captions'),
])
def test_artifact_of_assigns_metadata_failures_by_kind(phase, kind, artifact):
    """A failure's artifact follows its phase; metadata failures follow the job's primary artifact."""
    assert testee._artifact_of(phase, kind) == artifact
