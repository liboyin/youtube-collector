"""Tests for the SQLite store (DESIGN 5.1, 5.3, 5.7-5.10, 5.12 and section 7)."""

import json
import sqlite3
import threading
from contextlib import closing
from unittest.mock import patch

import pytest

import youtube_collector.store as testee
from youtube_collector.categories import Category

VIDEO = 'dQw4w9WgXcQ'
OTHER_VIDEO = 'aaaaaaaaaaa'
PLAYLIST = 'PLabc'


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


def submit_video(store, kind=testee.JobKind.VIDEOS_WITH_CAPTIONS, video_id=VIDEO, **keywords):
    """Submit a single-video job."""
    languages = None if kind is testee.JobKind.VIDEOS else ['en']
    return store.submit(
        kind=kind, url=f'https://youtu.be/{video_id}', target_type=testee.TargetType.VIDEO, target_id=video_id,
        languages=languages, **keywords,
    )


def submit_playlist(store, kind=testee.JobKind.VIDEOS, playlist_id=PLAYLIST, **keywords):
    """Submit a playlist job."""
    languages = None if kind is testee.JobKind.VIDEOS else ['en']
    return store.submit(
        kind=kind, url=f'https://www.youtube.com/playlist?list={playlist_id}', target_type=testee.TargetType.PLAYLIST,
        target_id=playlist_id, languages=languages, **keywords,
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


def all_revisions(db_path):
    """Return every revision stored in the record tables."""
    with closing(sqlite3.connect(db_path)) as connection:
        return sorted(row[0] for table in ('jobs', 'items', 'failures')
                      for row in connection.execute(f'SELECT revision FROM {table}'))


def job_item(store, job_id, position=0):
    """Return a job's live item at a position."""
    return store.get_job(job_id)['items'][position]


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
        job = submit_video(store).job
    testee.initialize(db_path)
    with testee.Store(db_path, clock) as store:
        assert store.get_job(job['id']) == job


def test_initialize_rejects_an_unknown_schema_version(tmp_path):
    """A database from another schema version is refused, not migrated blindly."""
    path = tmp_path / 'future.sqlite3'
    with closing(sqlite3.connect(path)) as connection:
        connection.execute('PRAGMA user_version = 99')
    with pytest.raises(RuntimeError, match='99'):
        testee.initialize(path)


def test_initialize_rolls_back_a_failed_schema_creation(tmp_path):
    """A failure while creating the schema leaves the file without a version, so a retry starts over."""
    path = tmp_path / 'broken.sqlite3'
    with patch.object(testee, '_SCHEMA', testee._SCHEMA + 'CREATE TABLE jobs (x);'), \
            pytest.raises(sqlite3.OperationalError):
        testee.initialize(path)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 0
        assert connection.execute("SELECT name FROM sqlite_master WHERE name = 'jobs'").fetchone() is None


def test_iso_formats_utc_timestamps():
    """Timestamps are ISO 8601 UTC, and None stays None."""
    assert testee._iso(0.0) == '1970-01-01T00:00:00+00:00'
    assert testee._iso(None) is None


def test_store_rejects_an_uninitialized_database(tmp_path, clock):
    """Opening a store on a file without the schema fails."""
    with pytest.raises(RuntimeError, match='schema version 0'):
        testee.Store(tmp_path / 'empty.sqlite3', clock)


def test_store_refuses_use_from_another_thread(store):
    """A connection belongs to the thread that opened it (decision C36)."""
    errors = []

    def use():
        try:
            store.list_jobs()
        except sqlite3.ProgrammingError as error:
            errors.append(error)

    thread = threading.Thread(target=use)
    thread.start()
    thread.join()
    assert len(errors) == 1


def test_store_closes_on_exit(db_path, clock):
    """Leaving the context closes the connection."""
    with testee.Store(db_path, clock) as store:
        pass
    with pytest.raises(sqlite3.ProgrammingError):
        store.list_jobs()


def test_write_rolls_back_every_statement_when_one_fails(store):
    """A write that fails part-way leaves no partial state and gives back the revisions it took."""
    job = submit_playlist(store).job
    store.start_expansion(job['id'])
    revision = store.current_revision()
    entry = testee.PlaylistEntry(VIDEO)
    with pytest.raises(sqlite3.IntegrityError), store._write() as connection:
        store._insert_item(connection, job['id'], testee.JobKind.VIDEOS, 0, entry, 0.0)
        store._insert_item(connection, job['id'], testee.JobKind.VIDEOS, 0, entry, 0.0)  # same position
    assert store.current_revision() == revision
    assert store.get_job(job['id'])['items'] == []


def test_current_revision_counts_every_change(store, db_path):
    """Every record change takes the next revision, and each row keeps its latest one."""
    assert store.current_revision() == 0
    job = submit_video(store).job
    assert (job['revision'], job['items'][0]['revision']) == (1, 2)
    store.start_item(job['items'][0]['id'])
    assert store.current_revision() == 4  # item, then its job leaving queued
    assert all_revisions(db_path) == [3, 4]


def test_pruned_through_starts_at_zero(store):
    """Nothing has been pruned in a new database."""
    assert store.pruned_through() == 0


def test_submit_creates_a_video_job_with_its_item(store, clock):
    """A single-video job gets its one queued item at submission."""
    submission = submit_video(store)
    job = submission.job
    assert submission.created
    assert job['kind'] == 'videos-with-captions'
    assert job['target'] == {'type': 'video', 'id': VIDEO}
    assert job['languages'] == ['en']
    assert job['state'] == 'queued'
    assert job['created_at'] == testee._iso(clock.now)
    assert not job['removed']
    [item] = job['items']
    assert (item['video_id'], item['state'], item['position'], item['title']) == (VIDEO, 'queued', 0, None)
    assert item['media'] == {'outcome': None, 'path': None}
    assert item['captions'] == {'outcome': None, 'path': None, 'track': None}
    assert item['progress'] is None


def test_submit_creates_a_playlist_job_without_items(store):
    """A playlist job has no items until expansion."""
    job = submit_playlist(store).job
    assert job['items'] == []
    assert job['target'] == {'type': 'playlist', 'id': PLAYLIST}
    assert job['languages'] is None


def test_submit_replays_the_same_key_and_body(store):
    """The same key and body return the earlier job without creating another."""
    first = submit_video(store, idempotency_key='k', body='b')
    again = submit_video(store, idempotency_key='k', body='b')
    assert not again.created
    assert again.job == first.job
    assert len(store.list_jobs()) == 1


def test_submit_rejects_the_same_key_with_another_body(store):
    """Reusing a key with a different body is a conflict, and nothing is created."""
    submit_video(store, idempotency_key='k', body='b')
    revision = store.current_revision()
    with pytest.raises(testee.IdempotencyConflict):
        submit_video(store, idempotency_key='k', body='other')
    assert store.current_revision() == revision
    assert len(store.list_jobs()) == 1


def test_submit_requires_a_body_with_a_key(store):
    """A key without a canonical body is a programming error."""
    with pytest.raises(ValueError):
        submit_video(store, idempotency_key='k')


def test_submit_without_a_key_always_creates(store):
    """Submissions without a key are never deduplicated."""
    assert submit_video(store).job['id'] != submit_video(store).job['id']


def test_submit_replays_the_final_representation_after_removal(store, clock):
    """After the job is removed, its key returns the stored final representation (C34)."""
    job = submit_video(store, kind=testee.JobKind.VIDEOS, idempotency_key='k', body='b').job
    item_id = job['items'][0]['id']
    store.start_item(item_id)
    store.finish_item(item_id, media_done())
    store.remove_items([item_id])
    assert store.remove_finished_jobs() == 1
    clock.advance(testee.KEY_RETENTION_SECONDS - 1)
    replay = submit_video(store, kind=testee.JobKind.VIDEOS, idempotency_key='k', body='b')
    assert not replay.created
    assert replay.job['id'] == job['id']
    assert replay.job['removed']
    assert replay.job['state'] == 'finished'
    with pytest.raises(testee.IdempotencyConflict):
        submit_video(store, idempotency_key='k', body='other')


def test_submit_forgets_an_expired_key(store, clock):
    """Once a removed job's key expires, reusing it creates a new job (C34)."""
    job = submit_playlist(store, idempotency_key='k', body='b').job
    store.start_expansion(job['id'])
    store.complete_expansion(job['id'], 'Empty', [])
    store.remove_finished_jobs()
    clock.advance(testee.KEY_RETENTION_SECONDS)
    again = submit_playlist(store, idempotency_key='k', body='other')
    assert again.created
    assert again.job['id'] != job['id']


def test_submit_keeps_a_key_while_its_job_is_live(store, clock):
    """A live job's key never expires, however old it is."""
    job = submit_video(store, idempotency_key='k', body='b').job
    clock.advance(365 * 24 * 3600)
    store.prune(0)
    assert submit_video(store, idempotency_key='k', body='b').job['id'] == job['id']


def test_get_job_returns_live_jobs_only(store):
    """Unknown and removed jobs are not in the status table."""
    assert store.get_job('missing') is None
    job = submit_playlist(store).job
    expand(store, job['id'], [])
    store.remove_finished_jobs()
    assert store.get_job(job['id']) is None


def test_list_jobs_returns_live_jobs_in_submission_order_with_live_items(store):
    """The status table lists jobs in order with only their live items."""
    first = submit_playlist(store).job
    second = submit_video(store).job
    expand(store, first['id'], [VIDEO, OTHER_VIDEO])
    item = job_item(store, first['id'], 0)
    store.start_item(item['id'])
    store.finish_item(item['id'], media_done())
    store.remove_items([item['id']])
    jobs = store.list_jobs()
    assert [job['id'] for job in jobs] == [first['id'], second['id']]
    assert [item['video_id'] for item in jobs[0]['items']] == [OTHER_VIDEO]


def test_list_failures_returns_uncleared_failures_oldest_first(store):
    """Failures are listed in creation order, and cleared ones disappear."""
    first = submit_playlist(store, playlist_id='PLa').job
    second = submit_playlist(store, playlist_id='PLb').job
    store.fail_expansion(first['id'], failure(testee.Phase.EXPANDING, message='a'))
    store.fail_expansion(second['id'], failure(testee.Phase.EXPANDING, message='b'))
    failures = store.list_failures()
    assert [f['message'] for f in failures] == ['a', 'b']
    store.clear_failure(failures[0]['id'])
    assert [f['message'] for f in store.list_failures()] == ['b']


def test_jobs_snapshot_returns_the_status_table_with_its_revision(store):
    """The status table comes with the revision it was read at (C38)."""
    job = submit_video(store).job
    snapshot = store.jobs_snapshot()
    assert [record['id'] for record in snapshot.records] == [job['id']]
    assert snapshot.records[0]['items'] == job['items']
    assert snapshot.revision == store.current_revision()


def test_failures_snapshot_returns_uncleared_failures_with_their_revision(store):
    """Uncleared failures come with the revision they were read at (C38)."""
    job = submit_playlist(store).job
    store.fail_expansion(job['id'], failure(testee.Phase.EXPANDING))
    snapshot = store.failures_snapshot()
    assert snapshot.records == tuple(store.list_failures())
    assert snapshot.revision == store.current_revision()


def test_job_view_counts_item_changes_and_removals(store):
    """A job's view revision rises with any of its items' changes, including removal."""
    job = submit_video(store, kind=testee.JobKind.VIDEOS).job
    item_id = job['items'][0]['id']
    assert store.job_view(job['id']).revision == job['items'][0]['revision']
    store.record_progress(item_id, 1, 2)
    assert store.job_view(job['id']).revision == store.current_revision()
    store.finish_item(item_id, media_done())
    store.remove_items([item_id])
    view = store.job_view(job['id'])
    assert view.revision == store.current_revision()
    assert view.job['items'] == []
    assert store.job_view('missing') is None


def test_job_view_of_a_playlist_without_items_uses_the_job_revision(store):
    """A job with no items yet has its own revision as its view revision."""
    job = submit_playlist(store).job
    assert store.job_view(job['id']).revision == job['revision']


def test_job_view_is_none_once_the_job_is_removed(store):
    """A removed job has no view; its tombstone reaches callers through the feed."""
    job = submit_playlist(store).job
    expand(store, job['id'], [])
    store.remove_finished_jobs()
    assert store.job_view(job['id']) is None


def test_changes_lists_each_record_once_at_its_latest_revision(store):
    """Coalesced pages hold the latest state of each changed record, in revision order (C38)."""
    job = submit_video(store, kind=testee.JobKind.VIDEOS).job
    item_id = job['items'][0]['id']
    store.start_item(item_id)
    store.record_progress(item_id, 1, 10)
    store.record_progress(item_id, 5, 10)
    page = store.changes(0, 100)
    assert [(e['kind'], e['id']) for e in page.entries] == [('job', job['id']), ('item', item_id)]
    revisions = [e['revision'] for e in page.entries]
    assert revisions == sorted(revisions)
    assert page.entries[1]['record']['progress']['downloaded_bytes'] == 5
    assert 'items' not in page.entries[0]['record']
    assert (page.cursor, page.more) == (store.current_revision(), False)


def test_changes_after_a_cursor_returns_only_later_changes(store):
    """Only records changed after the cursor appear."""
    first = submit_playlist(store, playlist_id='PLa').job
    cursor = store.current_revision()
    second = submit_playlist(store, playlist_id='PLb').job
    page = store.changes(cursor, 100)
    assert [e['id'] for e in page.entries] == [second['id']]
    assert first['id'] not in [e['id'] for e in page.entries]


def test_changes_include_tombstones_with_their_final_state(store):
    """A removal appears as a removed entry holding the record's final state (DESIGN 5.10)."""
    job = submit_playlist(store, idempotency_key='k', body='b').job
    store.fail_expansion(job['id'], failure(testee.Phase.EXPANDING, message='gone'))
    cursor = store.current_revision()
    [cleared] = store.clear_failures()
    store.remove_finished_jobs()
    page = store.changes(cursor, 100)
    assert [(e['kind'], e['removed']) for e in page.entries] == [('failure', True), ('job', True)]
    assert page.entries[0]['record'] == cleared
    assert page.entries[1]['record']['state'] == 'finished'


def test_changes_page_by_limit_and_continue_from_the_cursor(store):
    """A full page says more remain, and its cursor continues exactly where it stopped."""
    for index in range(5):
        submit_playlist(store, playlist_id=f'PL{index}')
    first = store.changes(0, 2)
    assert (len(first.entries), first.more) == (2, True)
    second = store.changes(first.cursor, 2)
    third = store.changes(second.cursor, 2)
    assert (len(third.entries), third.more) == (1, False)
    ids = [e['id'] for page in (first, second, third) for e in page.entries]
    assert len(ids) == len(set(ids)) == 5
    assert store.changes(third.cursor, 2) == testee.ChangePage((), third.cursor, False)
    exact = store.changes(first.cursor, 3)
    assert (len(exact.entries), exact.more) == (3, False)  # a page that is exactly full has no more


def test_changes_merge_kinds_into_one_revision_order_across_pages(store):
    """Entries of different kinds interleave by revision, also at page boundaries."""
    job = submit_video(store).job
    store.start_item(job['items'][0]['id'])
    playlist = submit_playlist(store).job
    store.fail_expansion(playlist['id'], failure(testee.Phase.EXPANDING))
    seen = []
    cursor = 0
    while True:
        page = store.changes(cursor, 1)
        seen.extend((e['kind'], e['revision']) for e in page.entries)
        cursor = page.cursor
        if not page.more:
            break
    assert [revision for _, revision in seen] == sorted(revision for _, revision in seen)
    assert [kind for kind, _ in seen] == ['item', 'job', 'job', 'failure']


def test_changes_move_a_record_that_changes_while_paging_forward(store):
    """A record that changes after its page was read reappears later, so its final state is never missed."""
    first = submit_playlist(store, playlist_id='PLa').job
    submit_playlist(store, playlist_id='PLb')
    page = store.changes(0, 1)
    assert page.entries[0]['id'] == first['id']
    store.start_expansion(first['id'])
    rest = store.changes(page.cursor, 10)
    assert [e['id'] for e in rest.entries][-1] == first['id']
    assert rest.entries[-1]['record']['state'] == 'expanding'


def test_changes_reject_cursors_below_the_pruned_through_revision(store, clock):
    """A cursor below P has missed pruned removals and gets CursorPruned; P itself is fine (C27)."""
    job = submit_playlist(store).job
    expand(store, job['id'], [])
    store.remove_finished_jobs()
    store.prune(0)
    mark = store.pruned_through()
    with pytest.raises(testee.CursorPruned) as raised:
        store.changes(mark - 1, 10)
    assert raised.value.pruned_through == mark
    assert store.changes(mark, 10).entries == ()


def test_changes_reject_a_limit_below_one(store):
    """A page must be allowed at least one entry."""
    with pytest.raises(ValueError):
        store.changes(0, 0)


def test_next_work_returns_nothing_without_work(store):
    """An empty store has no work."""
    assert store.next_work(0) is None


def test_next_work_expands_a_queued_playlist_first(store):
    """A queued playlist job's unit is its expansion."""
    job = submit_playlist(store, kind=testee.JobKind.CAPTIONS).job
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
    job = submit_playlist(store).job
    expand(store, job['id'], [VIDEO, OTHER_VIDEO], title='List')
    unit = store.next_work(0)
    assert unit.item == testee.WorkItem(job_item(store, job['id'], 0)['id'], VIDEO, f'T {VIDEO}')
    assert unit.playlist_title == 'List'
    store.start_item(unit.item.id)
    assert store.next_work(unit.job_seq).item.video_id == OTHER_VIDEO


def test_next_work_rotates_round_robin_across_jobs(store):
    """After a unit of one job, the next job in submission order goes next, wrapping around (C9)."""
    playlist = submit_playlist(store).job
    expand(store, playlist['id'], ['v' * 11, 'w' * 11, 'x' * 11])
    single = submit_video(store).job
    order = []
    after = 0
    while (unit := store.next_work(after)) is not None:
        order.append(unit.item.video_id)
        store.start_item(unit.item.id)
        store.finish_item(unit.item.id, media_done())
        after = unit.job_seq
    assert order == ['v' * 11, VIDEO, 'w' * 11, 'x' * 11]
    assert store.get_job(single['id'])['state'] == 'finished'


def test_next_work_skips_finished_and_removed_jobs(store):
    """Finished jobs have no work."""
    job = submit_playlist(store).job
    store.fail_expansion(job['id'], failure(testee.Phase.EXPANDING))
    assert store.next_work(0) is None


def test_start_expansion_marks_the_job_expanding(store):
    """Expansion moves the job to expanding."""
    job = submit_playlist(store).job
    store.start_expansion(job['id'])
    assert store.get_job(job['id'])['state'] == 'expanding'


def test_complete_expansion_persists_items_and_starts_the_job(store):
    """Entries become queued items in playlist order and the job runs (DESIGN 5.3)."""
    job = submit_playlist(store, kind=testee.JobKind.CAPTIONS).job
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
    job = submit_playlist(store).job
    expand(store, job['id'], [])
    assert store.get_job(job['id'])['state'] == 'finished'


def test_complete_expansion_supersedes_failures_of_the_same_playlist_only(store):
    """A successful expansion clears earlier expansion failures for that playlist (DESIGN 5.9)."""
    failed = submit_playlist(store).job
    other = submit_playlist(store, playlist_id='PLother').job
    store.fail_expansion(failed['id'], failure(testee.Phase.EXPANDING))
    store.fail_expansion(other['id'], failure(testee.Phase.EXPANDING))
    retry = submit_playlist(store).job
    expand(store, retry['id'], [VIDEO])
    [remaining] = store.list_failures()
    assert remaining['playlist_id'] == 'PLother'


def test_fail_expansion_finishes_the_job_with_a_failure(store):
    """A failed expansion finishes the job with no items and records the failure (DESIGN 5.7)."""
    job = submit_playlist(store, kind=testee.JobKind.CAPTIONS).job
    store.start_expansion(job['id'])
    store.fail_expansion(job['id'], failure(testee.Phase.EXPANDING, Category.UNAVAILABLE, 'gone'))
    assert store.get_job(job['id'])['state'] == 'finished'
    [record] = store.list_failures()
    assert {key: record[key] for key in ('job_id', 'job_kind', 'item_id', 'video_id', 'playlist_id', 'phase',
                                          'category', 'message', 'removed', 'removal')} == {
        'job_id': job['id'], 'job_kind': 'captions', 'item_id': None, 'video_id': None, 'playlist_id': PLAYLIST,
        'phase': 'expanding', 'category': 'unavailable', 'message': 'gone', 'removed': False, 'removal': None,
    }
    assert record['url'] == job['url']


def test_start_item_runs_the_item_and_its_queued_job(store):
    """Starting an item marks it and a queued job running."""
    job = submit_video(store).job
    store.start_item(job['items'][0]['id'])
    started = store.get_job(job['id'])
    assert (started['state'], started['items'][0]['state']) == ('running', 'running')


def test_start_item_leaves_a_running_job_alone(store):
    """A job that is already running takes no extra revision."""
    job = submit_playlist(store).job
    expand(store, job['id'], [VIDEO, OTHER_VIDEO])
    job_revision = store.get_job(job['id'])['revision']
    store.start_item(job_item(store, job['id'], 0)['id'])
    assert store.get_job(job['id'])['revision'] == job_revision


def test_record_progress_reports_bytes_and_percentage(store):
    """Progress carries bytes, and a percentage only when the total is known (C20)."""
    item_id = submit_video(store).job['items'][0]['id']
    store.start_item(item_id)
    store.record_progress(item_id, 250, 1000)
    assert job_item(store, store.list_jobs()[0]['id'])['progress'] == {
        'downloaded_bytes': 250, 'total_bytes': 1000, 'percent': 25.0,
    }
    store.record_progress(item_id, 300, None)
    assert job_item(store, store.list_jobs()[0]['id'])['progress'] == {
        'downloaded_bytes': 300, 'total_bytes': None, 'percent': None,
    }


def test_record_progress_takes_a_revision(store):
    """Each progress record is a change that waiting callers can see."""
    item_id = submit_video(store).job['items'][0]['id']
    before = store.current_revision()
    store.record_progress(item_id, 1, 2)
    assert store.current_revision() == before + 1


def test_finish_item_records_outcomes_and_finishes_the_job(store):
    """A finished item keeps its outcomes, paths and track, loses its progress, and ends its job."""
    job = submit_video(store).job
    item_id = job['items'][0]['id']
    store.start_item(item_id)
    store.record_progress(item_id, 5, 10)
    store.finish_item(item_id, testee.ItemResult(
        state=testee.ItemState.DONE, title='Title', media_outcome=testee.MediaOutcome.DOWNLOADED,
        media_path='Title [x].mp4', captions_outcome=testee.CaptionsOutcome.DOWNLOADED,
        captions_path='Title [x].srt', track_code='en-US', track_kind=testee.TrackKind.GENERATED,
    ))
    finished = store.get_job(job['id'])
    item = finished['items'][0]
    assert finished['state'] == 'finished'
    assert (item['state'], item['title'], item['progress']) == ('done', 'Title', None)
    assert item['media'] == {'outcome': 'downloaded', 'path': 'Title [x].mp4'}
    assert item['captions'] == {'outcome': 'downloaded', 'path': 'Title [x].srt',
                                'track': {'code': 'en-US', 'kind': 'generated'}}


def test_finish_item_keeps_the_listed_title_when_none_is_given(store):
    """A result without a title keeps the title the playlist listing supplied."""
    job = submit_playlist(store).job
    expand(store, job['id'], [VIDEO])
    item_id = job_item(store, job['id'])['id']
    store.finish_item(item_id, media_done())
    assert job_item(store, job['id'])['title'] == f'T {VIDEO}'


def test_finish_item_keeps_the_job_running_while_items_remain(store):
    """A job finishes only when no item is queued or running."""
    job = submit_playlist(store).job
    expand(store, job['id'], [VIDEO, OTHER_VIDEO])
    first, second = store.get_job(job['id'])['items']
    store.finish_item(first['id'], testee.ItemResult(state=testee.ItemState.FAILED, failures=[failure()]))
    assert store.get_job(job['id'])['state'] == 'running'
    store.start_item(second['id'])
    store.finish_item(second['id'], media_done())
    assert store.get_job(job['id'])['state'] == 'finished'


def test_finish_item_records_failures_with_their_context(store):
    """An item's failures carry its job, item, video and playlist context (DESIGN 5.9)."""
    job = submit_playlist(store, kind=testee.JobKind.VIDEOS_WITH_CAPTIONS).job
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
    item_id = submit_video(store).job['items'][0]['id']
    with pytest.raises(ValueError):
        store.finish_item(item_id, testee.ItemResult(state=testee.ItemState.RUNNING))


@pytest.mark.parametrize('outcome', [testee.MediaOutcome.DOWNLOADED, testee.MediaOutcome.EXISTING])
def test_finish_item_supersedes_media_failures_of_the_same_video(store, outcome):
    """A definitive media outcome clears earlier media and media-metadata failures for that video (DESIGN 5.9)."""
    for phase in (testee.Phase.MEDIA, testee.Phase.METADATA):
        old = submit_video(store, kind=testee.JobKind.VIDEOS).job['items'][0]['id']
        store.finish_item(old, testee.ItemResult(state=testee.ItemState.FAILED, failures=[failure(phase)]))
    other = submit_video(store, kind=testee.JobKind.VIDEOS, video_id=OTHER_VIDEO).job['items'][0]['id']
    store.finish_item(other, testee.ItemResult(state=testee.ItemState.FAILED, failures=[failure()]))
    retry = submit_video(store, kind=testee.JobKind.VIDEOS).job['items'][0]['id']
    store.finish_item(retry, media_done(outcome=outcome))
    assert [f['video_id'] for f in store.list_failures()] == [OTHER_VIDEO]


def test_finish_item_does_not_supersede_on_a_failed_outcome(store):
    """A failed media outcome is not definitive and clears nothing."""
    old = submit_video(store, kind=testee.JobKind.VIDEOS).job['items'][0]['id']
    store.finish_item(old, testee.ItemResult(state=testee.ItemState.FAILED, failures=[failure()]))
    retry = submit_video(store, kind=testee.JobKind.VIDEOS).job['items'][0]['id']
    store.finish_item(retry, testee.ItemResult(state=testee.ItemState.FAILED, media_outcome=testee.MediaOutcome.FAILED,
                                               failures=[failure()]))
    assert len(store.list_failures()) == 2


@pytest.mark.parametrize('outcome', [
    testee.CaptionsOutcome.DOWNLOADED, testee.CaptionsOutcome.EXISTING, testee.CaptionsOutcome.UNAVAILABLE,
])
def test_finish_item_supersedes_caption_failures_but_not_media_ones(store, outcome):
    """A definitive captions outcome clears caption failures only, keeping the video's media failures."""
    old = submit_video(store).job['items'][0]['id']
    store.finish_item(old, testee.ItemResult(state=testee.ItemState.FAILED, media_outcome=testee.MediaOutcome.FAILED,
                                             failures=[failure(testee.Phase.MEDIA)]))
    caption_old = submit_video(store, kind=testee.JobKind.CAPTIONS).job['items'][0]['id']
    store.finish_item(caption_old, testee.ItemResult(state=testee.ItemState.FAILED,
                                                     failures=[failure(testee.Phase.CAPTIONS)]))
    caption_metadata = submit_video(store, kind=testee.JobKind.CAPTIONS).job['items'][0]['id']
    store.finish_item(caption_metadata, testee.ItemResult(state=testee.ItemState.FAILED,
                                                          failures=[failure(testee.Phase.METADATA)]))
    retry = submit_video(store, kind=testee.JobKind.CAPTIONS).job['items'][0]['id']
    store.finish_item(retry, testee.ItemResult(state=testee.ItemState.DONE, captions_outcome=outcome))
    assert [f['phase'] for f in store.list_failures()] == ['media']


def test_finish_item_does_not_supersede_on_caption_errors(store):
    """A caption error is not definitive, so earlier caption failures stay."""
    old = submit_video(store, kind=testee.JobKind.CAPTIONS).job['items'][0]['id']
    store.finish_item(old, testee.ItemResult(state=testee.ItemState.FAILED, failures=[failure(testee.Phase.CAPTIONS)]))
    retry = submit_video(store).job['items'][0]['id']
    store.finish_item(retry, testee.ItemResult(
        state=testee.ItemState.DONE, media_outcome=testee.MediaOutcome.DOWNLOADED, media_path='a.mp4',
        captions_outcome=testee.CaptionsOutcome.ERROR, failures=[failure(testee.Phase.CAPTIONS)],
    ))
    assert [f['item_id'] for f in store.list_failures()] == [old, retry]


def test_finish_item_marks_superseded_failures_as_such(store, db_path):
    """A superseded failure becomes a tombstone that says why, with a new revision."""
    old = submit_video(store, kind=testee.JobKind.VIDEOS).job['items'][0]['id']
    store.finish_item(old, testee.ItemResult(state=testee.ItemState.FAILED, failures=[failure()]))
    retry = submit_video(store, kind=testee.JobKind.VIDEOS).job['items'][0]['id']
    store.finish_item(retry, media_done())
    with closing(sqlite3.connect(db_path)) as connection:
        removal, removed_at, revision = connection.execute(
            'SELECT removal, removed_at, revision FROM failures').fetchone()
    assert (removal, removed_at is not None) == ('superseded', True)
    assert revision < store.current_revision()
    assert len(set(all_revisions(db_path))) == len(all_revisions(db_path))


def test_finished_items_lists_done_and_failed_items_with_their_files(store):
    """Reconciliation sees terminal items with their recorded paths only."""
    job = submit_playlist(store, kind=testee.JobKind.VIDEOS_WITH_CAPTIONS).job
    expand(store, job['id'], [VIDEO, OTHER_VIDEO, 'c' * 11])
    first, second, third = store.get_job(job['id'])['items']
    store.finish_item(first['id'], testee.ItemResult(
        state=testee.ItemState.DONE, media_outcome=testee.MediaOutcome.DOWNLOADED, media_path='a.mp4',
        captions_outcome=testee.CaptionsOutcome.EXISTING, captions_path='a.srt'))
    store.finish_item(second['id'], testee.ItemResult(state=testee.ItemState.FAILED, failures=[failure()]))
    store.start_item(third['id'])
    assert store.finished_items() == [
        testee.ItemFiles(first['id'], job['id'], ('a.mp4', 'a.srt')),
        testee.ItemFiles(second['id'], job['id'], ()),
    ]


def test_remove_items_leaves_tombstones_for_terminal_items_only(store, db_path):
    """Only done or failed items are removed, each taking a revision."""
    job = submit_playlist(store).job
    expand(store, job['id'], [VIDEO, OTHER_VIDEO])
    first, second = store.get_job(job['id'])['items']
    store.finish_item(first['id'], media_done())
    assert store.remove_items([first['id'], second['id'], 'missing']) == 1
    assert store.remove_items([first['id']]) == 0
    assert [item['id'] for item in store.get_job(job['id'])['items']] == [second['id']]
    with closing(sqlite3.connect(db_path)) as connection:
        assert connection.execute('SELECT COUNT(*) FROM items WHERE removed_at IS NOT NULL').fetchone()[0] == 1


def test_removal_takes_a_new_revision(store, db_path):
    """Removing an item or a job stamps it with the next revision, so the change feed sees it (DESIGN 5.10)."""
    job = submit_video(store, kind=testee.JobKind.VIDEOS).job
    item_id = job['items'][0]['id']
    store.finish_item(item_id, media_done())
    revision = store.current_revision()
    store.remove_items([item_id])
    store.remove_finished_jobs()
    with closing(sqlite3.connect(db_path)) as connection:
        item_revision = connection.execute('SELECT revision FROM items').fetchone()[0]
        job_revision = connection.execute('SELECT revision FROM jobs').fetchone()[0]
    assert (item_revision, job_revision) == (revision + 1, revision + 2)


def test_remove_finished_jobs_requires_finished_without_items_or_failures(store):
    """A job is removed only when finished with no live items and no uncleared failure (C26)."""
    queued = submit_playlist(store, playlist_id='PLq').job
    expanding = submit_playlist(store, playlist_id='PLe').job
    store.start_expansion(expanding['id'])
    with_items = submit_video(store, kind=testee.JobKind.VIDEOS).job
    store.finish_item(with_items['items'][0]['id'], media_done())
    with_failure = submit_playlist(store, playlist_id='PLf').job
    store.fail_expansion(with_failure['id'], failure(testee.Phase.EXPANDING))
    empty = submit_playlist(store, playlist_id='PLz').job
    expand(store, empty['id'], [])
    assert store.remove_finished_jobs() == 1
    assert {job['id'] for job in store.list_jobs()} == {
        queued['id'], expanding['id'], with_items['id'], with_failure['id'],
    }
    store.clear_failures()
    store.remove_items([with_items['items'][0]['id']])
    assert store.remove_finished_jobs() == 2
    assert {job['id'] for job in store.list_jobs()} == {queued['id'], expanding['id']}


def test_remove_finished_jobs_stores_the_final_representation_with_the_key(store, db_path, clock):
    """Removal turns the key's record into the job's final state with an expiry (C24, C34)."""
    job = submit_playlist(store, idempotency_key='k', body='b').job
    expand(store, job['id'], [])
    store.remove_finished_jobs()
    with closing(sqlite3.connect(db_path)) as connection:
        final, expires_at = connection.execute('SELECT final, expires_at FROM idempotency_keys').fetchone()
    assert json.loads(final)['removed']
    assert json.loads(final)['id'] == job['id']
    assert expires_at == clock.now + testee.KEY_RETENTION_SECONDS


def test_clear_failures_returns_the_cleared_records(store):
    """Clearing everything returns each record as cleared."""
    job = submit_playlist(store).job
    store.fail_expansion(job['id'], failure(testee.Phase.EXPANDING))
    [cleared] = store.clear_failures()
    assert (cleared['removed'], cleared['removal']) == (True, 'cleared')
    assert store.list_failures() == []
    assert store.clear_failures() == []


def test_clear_failure_clears_one_failure(store):
    """Clearing one failure returns it once, then reports it unknown."""
    job = submit_playlist(store).job
    store.fail_expansion(job['id'], failure(testee.Phase.EXPANDING))
    [record] = store.list_failures()
    cleared = store.clear_failure(record['id'])
    assert cleared['id'] == record['id']
    assert cleared['revision'] > record['revision']
    assert store.clear_failure(record['id']) is None
    assert store.clear_failure('missing') is None


def test_prune_deletes_old_tombstones_and_records_the_highest_revision(store, clock, db_path):
    """Tombstones past the retention are deleted, and pruned-through rises to their highest revision (C27)."""
    old = submit_playlist(store, playlist_id='PLold').job
    expand(store, old['id'], [])
    store.remove_finished_jobs()
    old_revision = all_revisions(db_path)[-1]
    clock.advance(100)
    young = submit_playlist(store, playlist_id='PLyoung').job
    expand(store, young['id'], [])
    store.remove_finished_jobs()
    assert store.prune(100) == 1
    assert store.pruned_through() == old_revision
    with closing(sqlite3.connect(db_path)) as connection:
        assert [row[0] for row in connection.execute('SELECT id FROM jobs')] == [young['id']]


def test_prune_keeps_live_records(store, clock):
    """Live jobs, items and failures are never pruned, however old."""
    job = submit_video(store).job
    clock.advance(10_000)
    assert store.prune(0) == 0
    assert store.get_job(job['id']) is not None
    assert store.pruned_through() == 0


def test_prune_never_lowers_pruned_through(store, clock):
    """A later prune of older revisions cannot move the mark back."""
    job = submit_playlist(store).job
    expand(store, job['id'], [])
    store.remove_finished_jobs()
    store.prune(0)
    mark = store.pruned_through()
    assert mark > 0
    store.prune(0)
    assert store.pruned_through() == mark


def test_prune_deletes_expired_keys(store, clock, db_path):
    """Expired keys are deleted; retained ones stay."""
    job = submit_playlist(store, idempotency_key='k', body='b').job
    expand(store, job['id'], [])
    store.remove_finished_jobs()
    clock.advance(testee.KEY_RETENTION_SECONDS - 1)
    store.prune(10 ** 9)
    with closing(sqlite3.connect(db_path)) as connection:
        assert connection.execute('SELECT COUNT(*) FROM idempotency_keys').fetchone()[0] == 1
    clock.advance(1)
    store.prune(10 ** 9)
    with closing(sqlite3.connect(db_path)) as connection:
        assert connection.execute('SELECT COUNT(*) FROM idempotency_keys').fetchone()[0] == 0


def test_recover_requeues_running_items_and_expanding_jobs(store):
    """After a restart, running items and expanding jobs start over (DESIGN 5.12)."""
    video = submit_video(store).job
    item_id = video['items'][0]['id']
    store.start_item(item_id)
    store.record_progress(item_id, 5, 10)
    playlist = submit_playlist(store).job
    store.start_expansion(playlist['id'])
    revision = store.current_revision()
    store.recover()
    recovered = store.get_job(video['id'])['items'][0]
    assert (recovered['state'], recovered['progress']) == ('queued', None)
    assert store.get_job(playlist['id'])['state'] == 'queued'
    assert store.current_revision() == revision + 2
    assert store.next_work(0).item.id == item_id


def test_update_rejects_unknown_rows(store):
    """Writing to a row that does not exist is an error, and the write is rolled back."""
    revision = store.current_revision()
    with pytest.raises(LookupError):
        store.start_expansion('missing')
    assert store.current_revision() == revision


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
