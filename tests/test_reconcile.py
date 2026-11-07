"""Tests for the reconciliation thread (DESIGN 5.8, 5.13, 5.14; C25, C26)."""

import logging
import sqlite3
import threading
import types
from unittest.mock import patch

import pytest

import youtube_collector.reconcile as testee
from youtube_collector import staging, store

VIDEO = 'abcdefghijk'


class Clock:
    """A manually advanced clock."""

    def __init__(self, now):
        """Start at a fixed time."""
        self.now = now

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
def wall():
    """Provide the wall clock shared by the store and the reconciler."""
    return Clock(1_000_000.0)


@pytest.fixture
def db_path(tmp_path):
    """Provide an initialized database file owned by the test."""
    path = tmp_path / 'db.sqlite3'
    store.initialize(path)
    return path


@pytest.fixture
def db(db_path, wall):
    """Provide the test thread's store."""
    with store.Store(db_path, wall) as opened:
        yield opened


@pytest.fixture
def notified():
    """Count notifications."""
    return []


@pytest.fixture
def monotonic():
    """Provide the monotonic clock."""
    return Clock(50.0)


@pytest.fixture
def reconciler(db_path, root, wall, monotonic, notified):
    """Provide a reconciler on the test's store and staging root."""
    return testee.Reconciler(
        open_store=lambda: store.Store(db_path, wall), staging_root=root, notify=lambda: notified.append(1),
        clock=wall, monotonic=monotonic, interval=60, tombstone_retention=100,
    )


def finished_item(db, media_path=None, captions_path=None, video_id=VIDEO):
    """Submit a single-video job and finish its item with the given recorded files; return the job ID."""
    job = db.submit(kind=store.JobKind.VIDEOS_WITH_CAPTIONS, url='u', target_type=store.TargetType.VIDEO,
                    target_id=video_id, languages=['en']).job
    db.finish_item(job['items'][0]['id'], store.ItemResult(
        state=store.ItemState.DONE, media_outcome=store.MediaOutcome.DOWNLOADED, media_path=media_path,
        captions_outcome=store.CaptionsOutcome.UNAVAILABLE, captions_path=captions_path,
    ))
    return job['id']


def staging_with(**overrides):
    """Return a stand-in for the staging module with some functions replaced."""
    return types.SimpleNamespace(**{**vars(staging), **overrides})


def test_run_pass_removes_items_whose_files_are_gone_and_their_jobs(reconciler, db, root, notified, wall):
    """Items whose recorded files are all gone are removed, then their finished jobs (DESIGN 5.8, C26)."""
    (root / 'kept.mp4').touch()
    (root / 'kept-srt.srt').touch()
    kept = finished_item(db, media_path='kept.mp4')
    half = finished_item(db, media_path='moved.mp4', captions_path='kept-srt.srt')
    gone = finished_item(db, media_path='moved.mp4', captions_path='moved.srt')
    nothing = finished_item(db)
    result = reconciler.run_pass(db)
    assert result == testee.PassResult(reconciled=True, removed_items=2, removed_jobs=2, pruned=0)
    assert {job['id'] for job in db.list_jobs()} == {kept, half}
    assert gone not in {job['id'] for job in db.list_jobs()}
    assert nothing not in {job['id'] for job in db.list_jobs()}
    assert notified == [1]
    assert reconciler.last_reconciled_at() == wall.now


def test_run_pass_keeps_live_items_and_their_jobs(reconciler, db):
    """Queued and running items are never removed, so their jobs stay."""
    job = db.submit(kind=store.JobKind.VIDEOS, url='u', target_type=store.TargetType.VIDEO, target_id=VIDEO,
                    languages=None).job
    playlist = db.submit(kind=store.JobKind.VIDEOS, url='p', target_type=store.TargetType.PLAYLIST,
                         target_id='PL', languages=None).job
    db.start_expansion(playlist['id'])
    assert reconciler.run_pass(db).removed_items == 0
    assert {j['id'] for j in db.list_jobs()} == {job['id'], playlist['id']}


def test_run_pass_without_changes_does_not_notify(reconciler, db, notified):
    """A pass that changes nothing wakes nobody."""
    reconciler.run_pass(db)
    assert notified == []


def test_run_pass_notifies_committed_removals_when_a_later_write_fails(reconciler, db, notified):
    """Removals that committed are announced even when pruning then fails, and the error still propagates."""
    finished_item(db, media_path='gone.mp4')
    ticket = reconciler.request_pass()
    with patch.object(db, 'prune', side_effect=sqlite3.OperationalError('database is locked')), \
            pytest.raises(sqlite3.OperationalError):
        reconciler.run_pass(db)
    assert db.list_jobs() == []
    assert notified == [1]
    assert reconciler.wait_for_pass(ticket, 0)
    assert reconciler.last_reconciled_at() is None


def test_run_pass_changes_nothing_while_the_marker_is_missing(reconciler, db, root, wall, monotonic, caplog):
    """An unmounted share pauses reconciliation without removing anything (DESIGN 5.13)."""
    job = finished_item(db, media_path='gone.mp4')
    (root / staging.MARKER_NAME).unlink()
    revision = db.current_revision()
    probed = []
    with caplog.at_level(logging.WARNING, logger=testee.__name__), \
            patch.object(testee, 'staging', staging_with(file_present=probed.append)):
        assert reconciler.run_pass(db) == testee.PassResult(reconciled=False)
        assert reconciler.run_pass(db) == testee.PassResult(reconciled=False)
        assert probed == []  # an unavailable share is not probed at all
        assert db.current_revision() == revision
        assert reconciler.last_reconciled_at() is None
        assert reconciler.staging_check() == testee.StagingCheck(False, monotonic.now)
        (root / staging.MARKER_NAME).touch()
        monotonic.now += 5
        assert reconciler.run_pass(db).reconciled
    assert reconciler.staging_check() == testee.StagingCheck(True, monotonic.now)
    assert [record.message.split('; ')[1] for record in caplog.records] == [
        'reconciliation paused', 'reconciliation resumed',
    ]
    assert db.get_job(job) is None


def test_run_pass_changes_nothing_when_the_share_disappears_mid_pass(reconciler, db, root):
    """A share lost while probing makes every file look gone; the second marker check prevents removal."""
    finished_item(db, media_path='present.mp4')
    (root / 'present.mp4').touch()

    def unmount_then_check(path):
        (root / staging.MARKER_NAME).unlink()
        (root / 'present.mp4').unlink()
        return staging.file_present(path)

    revision = db.current_revision()
    with patch.object(testee, 'staging', staging_with(file_present=unmount_then_check)):
        assert reconciler.run_pass(db) == testee.PassResult(reconciled=False)
    assert db.current_revision() == revision
    assert reconciler.staging_check().marker_present is False


def test_run_pass_keeps_items_whose_files_cannot_be_checked(reconciler, db, root, caplog):
    """A file check that fails, rather than finding nothing, keeps the item."""
    loop = root / 'loop.mp4'
    loop.symlink_to(loop)
    job = finished_item(db, media_path='loop.mp4')
    with caplog.at_level(logging.WARNING, logger=testee.__name__):
        assert reconciler.run_pass(db).removed_items == 0
    assert db.get_job(job) is not None
    assert 'keeping it' in caplog.text


def test_run_pass_prunes_old_tombstones(reconciler, db, wall):
    """Tombstones past the retention are pruned on every pass."""
    finished_item(db, media_path='gone.mp4')
    reconciler.run_pass(db)
    wall.now += 100
    assert reconciler.run_pass(db).pruned == 2
    assert db.pruned_through() > 0


def test_request_pass_names_a_pass_that_starts_later(reconciler, db):
    """A ticket is satisfied only by a pass that started after the request."""
    ticket = reconciler.request_pass()
    assert not reconciler.wait_for_pass(ticket, 0)
    reconciler.run_pass(db)
    assert reconciler.wait_for_pass(ticket, 0)


def test_request_pass_during_a_pass_waits_for_the_next(reconciler, db, root):
    """A request made while a pass is probing is not satisfied by that pass, whose probe may predate it."""
    finished_item(db, media_path='a.mp4')
    tickets = []

    def request_while_probing(path):
        tickets.append(reconciler.request_pass())
        return staging.file_present(path)

    with patch.object(testee, 'staging', staging_with(file_present=request_while_probing)):
        reconciler.run_pass(db)
    assert not reconciler.wait_for_pass(tickets[0], 0)
    reconciler.run_pass(db)
    assert reconciler.wait_for_pass(tickets[0], 0)


def test_a_stalled_probe_does_not_block_api_reads(reconciler, db_path, wall):
    """While a staging probe is blocked, every call the API makes returns promptly (C25)."""
    with store.Store(db_path, wall) as db:
        finished_item(db, media_path='a.mp4')
    entered = threading.Event()
    release = threading.Event()

    def stalled(path):
        entered.set()
        release.wait(10)
        return False

    def run_one_pass():
        with store.Store(db_path, wall) as own:
            reconciler.run_pass(own)

    api_results = []
    api_done = threading.Event()

    def use_api():
        ticket = reconciler.request_pass()
        api_results.extend([
            reconciler.last_reconciled_at(), reconciler.staging_check().marker_present,
            reconciler.wait_for_pass(ticket, 0.01),
        ])
        api_done.set()

    with patch.object(testee, 'staging', staging_with(file_present=stalled)):
        thread = threading.Thread(target=run_one_pass)
        api = threading.Thread(target=use_api)
        thread.start()
        try:
            assert entered.wait(10)
            api.start()
            # The API calls must finish while the probe is still blocked.
            assert api_done.wait(5)
            assert api_results == [None, True, False]
        finally:
            release.set()
            thread.join(10)
            if api.ident is not None:
                api.join(10)
    assert not thread.is_alive()
    assert not api.is_alive()


def test_run_passes_at_start_and_on_request_until_stopped(reconciler, root, db):
    """The thread runs a pass at once, another on request, and stops on request."""
    finished_item(db, media_path='gone.mp4')
    thread = threading.Thread(target=reconciler.run)
    thread.start()
    try:
        assert reconciler.wait_for_pass(1, 10)
        assert db.list_jobs() == []
        assert reconciler.wait_for_pass(reconciler.request_pass(), 10)
    finally:
        reconciler.stop()
        thread.join(10)
    assert not thread.is_alive()


def test_run_survives_a_failing_pass_and_releases_waiters(reconciler, caplog):
    """An unexpected error is logged, the waiters are released, and the loop goes on."""
    calls = []
    released = []

    def failing(store_):
        calls.append(1)
        if len(calls) == 1:
            reconciler.request_pass()  # ends the loop's interval wait at once
            raise RuntimeError('database is locked')
        released.append(reconciler.wait_for_pass(1, 0))  # the failed pass released its waiters itself
        reconciler.stop()
        return testee.PassResult(reconciled=True)

    with patch.object(reconciler, '_reconcile', side_effect=failing), \
            caplog.at_level(logging.ERROR, logger=testee.__name__):
        reconciler.run()
    assert len(calls) == 2
    assert released == [True]
    assert reconciler.wait_for_pass(2, 0)
    assert 'Reconciliation pass failed' in caplog.text
