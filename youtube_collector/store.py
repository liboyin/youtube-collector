"""The SQLite store: jobs, items, failures, idempotency keys, revisions and tombstones.

SQLite is the source of truth (DESIGN 5.10). Every write is one short ``BEGIN IMMEDIATE``
transaction that also assigns revisions (decision C36): each record it changes, including a removal,
takes the next value of one global counter, so a record's row always carries its latest revision.
Removal sets ``removed_at`` and leaves the row as a tombstone holding the record's final state,
until ``prune`` deletes it.

Each thread opens its own ``Store``; sqlite3's thread check stays on, so a connection used from
another thread raises instead of corrupting a transaction.
"""

import json
import sqlite3
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Self

from youtube_collector.categories import Category

__all__ = [
    'KEY_RETENTION_SECONDS', 'SCHEMA_VERSION', 'CaptionsOutcome', 'ChangePage', 'CursorPruned', 'IdempotencyConflict',
    'ItemFiles', 'ItemResult', 'JobView', 'Snapshot',
    'ItemState', 'JobKind', 'JobState', 'MediaOutcome', 'NewFailure', 'Phase', 'PlaylistEntry', 'Store',
    'Submission', 'TargetType', 'TrackKind', 'WorkItem', 'WorkUnit', 'initialize',
]

SCHEMA_VERSION = 1
# DESIGN 5.1 and C24: keys outlive their job by at least seven days, whatever the tombstone retention is.
KEY_RETENTION_SECONDS = 7 * 24 * 60 * 60
_BUSY_TIMEOUT_SECONDS = 30.0

type Record = dict[str, object]


class JobKind(StrEnum):
    """What a job downloads (DESIGN 5.1)."""

    VIDEOS = 'videos'
    CAPTIONS = 'captions'
    VIDEOS_WITH_CAPTIONS = 'videos-with-captions'

    @property
    def wants_media(self) -> bool:
        """Whether items of this kind download MP4 media."""
        return self is not JobKind.CAPTIONS

    @property
    def wants_captions(self) -> bool:
        """Whether items of this kind fetch SRT captions."""
        return self is not JobKind.VIDEOS


class TargetType(StrEnum):
    """Whether a job names one video or a playlist."""

    VIDEO = 'video'
    PLAYLIST = 'playlist'


class JobState(StrEnum):
    """A job's lifecycle state (DESIGN 5.7)."""

    QUEUED = 'queued'
    EXPANDING = 'expanding'
    RUNNING = 'running'
    FINISHED = 'finished'


class ItemState(StrEnum):
    """An item's lifecycle state (DESIGN 5.7)."""

    QUEUED = 'queued'
    RUNNING = 'running'
    DONE = 'done'
    FAILED = 'failed'


class MediaOutcome(StrEnum):
    """What happened to an item's media (DESIGN 5.7)."""

    DOWNLOADED = 'downloaded'
    EXISTING = 'existing'
    FAILED = 'failed'


class CaptionsOutcome(StrEnum):
    """What happened to an item's captions (DESIGN 5.7)."""

    DOWNLOADED = 'downloaded'
    EXISTING = 'existing'
    UNAVAILABLE = 'unavailable'
    ERROR = 'error'
    FAILED = 'failed'


class TrackKind(StrEnum):
    """Whether the caption track an item used was created by a person or generated (DESIGN 5.6)."""

    MANUAL = 'manual'
    GENERATED = 'generated'


class Phase(StrEnum):
    """The step during which a failure happened (DESIGN 5.9)."""

    EXPANDING = 'expanding'
    METADATA = 'metadata'
    MEDIA = 'media'
    CAPTIONS = 'captions'


class _Artifact(StrEnum):
    """What a failure is about, which decides what supersedes it."""

    MEDIA = 'media'
    CAPTIONS = 'captions'
    PLAYLIST = 'playlist'


_DEFINITIVE_MEDIA = frozenset({MediaOutcome.DOWNLOADED, MediaOutcome.EXISTING})
_DEFINITIVE_CAPTIONS = frozenset({CaptionsOutcome.DOWNLOADED, CaptionsOutcome.EXISTING, CaptionsOutcome.UNAVAILABLE})


class IdempotencyConflict(Exception):
    """Raised when an idempotency key is reused with a different request body."""


class CursorPruned(Exception):
    """Raised when a change-feed cursor is below the pruned-through revision (decision C27).

    Attributes:
        pruned_through: The highest revision pruning has deleted.
    """

    def __init__(self, pruned_through: int) -> None:
        """Record the pruned-through revision."""
        super().__init__(f'Changes up to revision {pruned_through} were pruned; resynchronize')
        self.pruned_through = pruned_through


@dataclass(frozen=True)
class NewFailure:
    """A failure to record; the store adds its job, item, URL and playlist context.

    Attributes:
        phase: The step that failed.
        category: The hint at whether resubmitting helps.
        message: The raw error text.
    """

    phase: Phase
    category: Category
    message: str


@dataclass(frozen=True)
class PlaylistEntry:
    """One video of an expanded playlist, in playlist order.

    Attributes:
        video_id: The entry's video ID.
        title: Its title when the playlist listing supplied one.
    """

    video_id: str
    title: str | None = None


@dataclass(frozen=True)
class ItemResult:
    """How an item ended; fields for artifacts the job does not request stay None.

    Attributes:
        state: ``DONE`` or ``FAILED``.
        title: The video's title, when known.
        media_outcome: The media outcome.
        media_path: The staging-relative path of the media written or found existing.
        captions_outcome: The captions outcome.
        captions_path: The staging-relative path of the captions written or found existing.
        track_code: The language code of the caption track used.
        track_kind: Whether that track was manual or generated.
        failures: Failures this item produced.
    """

    state: ItemState
    title: str | None = None
    media_outcome: MediaOutcome | None = None
    media_path: str | None = None
    captions_outcome: CaptionsOutcome | None = None
    captions_path: str | None = None
    track_code: str | None = None
    track_kind: TrackKind | None = None
    failures: Sequence[NewFailure] = ()


@dataclass(frozen=True)
class WorkItem:
    """A queued item for the worker.

    Attributes:
        id: The item's ID.
        video_id: The video to process.
        title: Its title when the playlist listing supplied one.
    """

    id: str
    video_id: str
    title: str | None


@dataclass(frozen=True)
class WorkUnit:
    """The next unit of work: a playlist to expand (``item`` is None) or an item to process.

    Attributes:
        job_seq: The job's submission sequence number, the worker's round-robin cursor.
        job_id: The job's ID.
        kind: The job's kind.
        target_type: Whether the job names a video or a playlist.
        target_id: The video or playlist ID.
        languages: Caption language candidates, or None for a ``videos`` job.
        playlist_title: The playlist's title once expanded.
        item: The item to process, or None when the playlist must be expanded first.
    """

    job_seq: int
    job_id: str
    kind: JobKind
    target_type: TargetType
    target_id: str
    languages: tuple[str, ...] | None
    playlist_title: str | None
    item: WorkItem | None


@dataclass(frozen=True)
class ItemFiles:
    """A done or failed item and the staging-relative files it produced or found existing.

    Attributes:
        item_id: The item's ID.
        job_id: Its job's ID.
        paths: Its recorded media and captions paths; empty when it recorded none.
    """

    item_id: str
    job_id: str
    paths: tuple[str, ...]


@dataclass(frozen=True)
class Submission:
    """The result of a submission.

    Attributes:
        job: The job's representation, with its items.
        created: False when an idempotency key returned an earlier job.
    """

    job: Record
    created: bool


@dataclass(frozen=True)
class ChangePage:
    """One page of the change feed (DESIGN 5.10, decision C38).

    Attributes:
        entries: Changed records, each once in its latest state, ordered by revision. Each entry has
            ``kind`` (``job``, ``item`` or ``failure``), ``id``, ``revision``, ``removed`` and ``record``;
            job records carry no items.
        cursor: The ``since`` for the next page: the last revision on this page, or the requested
            ``since`` when the page is empty.
        more: Whether further changes remain beyond this page.
    """

    entries: tuple[Record, ...]
    cursor: int
    more: bool


@dataclass(frozen=True)
class Snapshot:
    """Records read in one transaction, with the revision they were read at (decision C38).

    Attributes:
        records: The records.
        revision: The highest revision assigned when they were read.
    """

    records: tuple[Record, ...]
    revision: int


@dataclass(frozen=True)
class JobView:
    """A live job with its items, and the revision of that view.

    Attributes:
        job: The job's representation with its live items.
        revision: The highest revision among the job and all its items, removed ones included, so an
            item's removal counts as a change to the job.
    """

    job: Record
    revision: int


_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL) STRICT;
INSERT INTO meta (key, value) VALUES ('revision', 0), ('pruned_through', 0);
CREATE TABLE jobs (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    url TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    languages TEXT,
    state TEXT NOT NULL,
    playlist_title TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    revision INTEGER NOT NULL UNIQUE,
    removed_at REAL
) STRICT;
CREATE TABLE items (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    job_kind TEXT NOT NULL,
    position INTEGER NOT NULL,
    video_id TEXT NOT NULL,
    title TEXT,
    state TEXT NOT NULL,
    media_outcome TEXT,
    media_path TEXT,
    captions_outcome TEXT,
    captions_path TEXT,
    track_code TEXT,
    track_kind TEXT,
    downloaded_bytes INTEGER,
    total_bytes INTEGER,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    revision INTEGER NOT NULL UNIQUE,
    removed_at REAL,
    UNIQUE (job_id, position)
) STRICT;
CREATE INDEX items_by_job ON items (job_id, position);
CREATE TABLE failures (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    job_id TEXT NOT NULL,
    job_kind TEXT NOT NULL,
    item_id TEXT,
    url TEXT NOT NULL,
    video_id TEXT,
    playlist_id TEXT,
    playlist_title TEXT,
    phase TEXT NOT NULL,
    artifact TEXT NOT NULL,
    category TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    revision INTEGER NOT NULL UNIQUE,
    removed_at REAL,
    removal TEXT
) STRICT;
CREATE INDEX failures_by_video ON failures (video_id, artifact);
CREATE INDEX failures_by_job ON failures (job_id);
CREATE TABLE idempotency_keys (
    key TEXT PRIMARY KEY,
    body TEXT NOT NULL,
    job_id TEXT NOT NULL,
    final TEXT,
    expires_at REAL
) STRICT;
"""


def initialize(path: Path) -> None:
    """Create the database at ``path`` if needed, enable WAL mode and check the schema version.

    Call once at startup, before any thread opens a ``Store``.

    Raises:
        RuntimeError: If the file holds a schema version this code does not know.
        sqlite3.Error: If the database cannot be opened or written.
    """
    connection = sqlite3.connect(path, timeout=_BUSY_TIMEOUT_SECONDS, autocommit=True)
    try:
        connection.execute('PRAGMA journal_mode = WAL')
        connection.execute('BEGIN IMMEDIATE')
        try:
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            if version == 0:
                connection.executescript(_SCHEMA)
                connection.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
            elif version != SCHEMA_VERSION:
                raise RuntimeError(f'Unknown database schema version {version} in {path}')
        except BaseException:
            connection.execute('ROLLBACK')
            raise
        connection.execute('COMMIT')
    finally:
        connection.close()


def _iso(timestamp: float | None) -> str | None:
    """Format an epoch timestamp as ISO 8601 UTC, or pass None through."""
    return None if timestamp is None else datetime.fromtimestamp(timestamp, UTC).isoformat()


class Store:
    """One thread's connection to the database, with every read and write the service needs.

    Args:
        path: The database file, already set up by ``initialize``.
        clock: Returns the current time as epoch seconds; injected so tests control it.
    """

    def __init__(self, path: Path, clock: Callable[[], float]) -> None:
        """Open the connection; ``initialize`` must have created the schema."""
        self._clock = clock
        self._connection = sqlite3.connect(path, timeout=_BUSY_TIMEOUT_SECONDS, autocommit=True)
        self._connection.row_factory = sqlite3.Row
        version = self._connection.execute('PRAGMA user_version').fetchone()[0]
        if version != SCHEMA_VERSION:
            self._connection.close()
            raise RuntimeError(f'Database {path} has schema version {version}, expected {SCHEMA_VERSION}')

    def close(self) -> None:
        """Close the connection."""
        self._connection.close()

    def __enter__(self) -> Self:
        """Return the store, which closes on exit."""
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None,
    ) -> None:
        """Close the connection."""
        self.close()

    # Transactions and revisions

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """Run the body as one immediate write transaction, rolled back if it raises."""
        self._connection.execute('BEGIN IMMEDIATE')
        try:
            yield self._connection
        except BaseException:
            self._connection.execute('ROLLBACK')
            raise
        self._connection.execute('COMMIT')

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        """Run the body as one read transaction, so every statement sees the same snapshot."""
        self._connection.execute('BEGIN')
        try:
            yield self._connection
        finally:
            self._connection.execute('COMMIT')

    @staticmethod
    def _next_revision(connection: sqlite3.Connection) -> int:
        """Take the next revision; only call inside a write transaction."""
        row = connection.execute("UPDATE meta SET value = value + 1 WHERE key = 'revision' RETURNING value").fetchone()
        revision: int = row[0]
        return revision

    def current_revision(self) -> int:
        """Return the highest revision assigned so far."""
        return self._meta('revision')

    def pruned_through(self) -> int:
        """Return the highest revision that pruning has deleted (decision C27)."""
        return self._meta('pruned_through')

    def _meta(self, key: str) -> int:
        """Read one counter from the meta table."""
        return self._read_meta(self._connection, key)

    # Submission

    def submit(
        self, *, kind: JobKind, url: str, target_type: TargetType, target_id: str,
        languages: Sequence[str] | None, idempotency_key: str | None = None, body: str | None = None,
    ) -> Submission:
        """Create a job, or return the job an idempotency key already names (DESIGN 5.1).

        A single-video job gets its one item at once; a playlist job gets its items on expansion.
        With a key, the same key and body return the earlier job: live with its items, or its final
        representation once it has been removed (decision C34). An expired key is forgotten first.

        Args:
            kind: The job kind.
            url: The URL as submitted.
            target_type: Whether the URL names a video or a playlist.
            target_id: The video or playlist ID.
            languages: Caption language candidates, or None for a ``videos`` job.
            idempotency_key: The caller's key, if any.
            body: The canonical request body the key is compared on; required with a key.

        Returns:
            The job and whether it was created.

        Raises:
            IdempotencyConflict: If the key is retained with a different body.
            ValueError: If a key is given without a body.
        """
        if idempotency_key is not None and body is None:
            raise ValueError('An idempotency key needs the canonical request body')
        with self._write() as connection:
            now = self._clock()
            if idempotency_key is not None:
                connection.execute(
                    'DELETE FROM idempotency_keys WHERE key = ? AND expires_at <= ?', (idempotency_key, now),
                )
                key_row = connection.execute(
                    'SELECT body, job_id, final FROM idempotency_keys WHERE key = ?', (idempotency_key,),
                ).fetchone()
                if key_row is not None:
                    if key_row['body'] != body:
                        raise IdempotencyConflict(f'Idempotency key {idempotency_key!r} was used with another body')
                    if key_row['final'] is not None:
                        return Submission(json.loads(key_row['final']), created=False)
                    return Submission(self._job_record(connection, key_row['job_id']), created=False)
            job_id = uuid.uuid4().hex
            connection.execute(
                'INSERT INTO jobs (id, kind, url, target_type, target_id, languages, state, created_at, updated_at,'
                ' revision) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (job_id, kind, url, target_type, target_id, None if languages is None else json.dumps(list(languages)),
                 JobState.QUEUED, now, now, self._next_revision(connection)),
            )
            if target_type is TargetType.VIDEO:
                self._insert_item(connection, job_id, kind, 0, PlaylistEntry(target_id), now)
            if idempotency_key is not None:
                connection.execute(
                    'INSERT INTO idempotency_keys (key, body, job_id) VALUES (?, ?, ?)',
                    (idempotency_key, body, job_id),
                )
            return Submission(self._job_record(connection, job_id), created=True)

    def _insert_item(
        self, connection: sqlite3.Connection, job_id: str, kind: JobKind, position: int, entry: PlaylistEntry,
        now: float,
    ) -> None:
        """Insert one queued item with the next revision."""
        connection.execute(
            'INSERT INTO items (id, job_id, job_kind, position, video_id, title, state, created_at, updated_at,'
            ' revision) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (uuid.uuid4().hex, job_id, kind, position, entry.video_id, entry.title, ItemState.QUEUED, now, now,
             self._next_revision(connection)),
        )

    # Reads

    def get_job(self, job_id: str) -> Record | None:
        """Return a job in the status table with its items, or None when it is unknown or removed."""
        with self._read() as connection:
            row = connection.execute('SELECT * FROM jobs WHERE id = ? AND removed_at IS NULL', (job_id,)).fetchone()
            return None if row is None else self._job_record(connection, job_id)

    def list_jobs(self) -> list[Record]:
        """Return the status table: every job not removed, in submission order, with its items."""
        with self._read() as connection:
            ids = [row['id'] for row in connection.execute('SELECT id FROM jobs WHERE removed_at IS NULL ORDER BY seq')]
            return [self._job_record(connection, job_id) for job_id in ids]

    def list_failures(self) -> list[Record]:
        """Return every uncleared failure, oldest first."""
        rows = self._connection.execute('SELECT * FROM failures WHERE removed_at IS NULL ORDER BY seq')
        return [_failure_record(row) for row in rows]

    def jobs_snapshot(self) -> Snapshot:
        """Return the status table with the revision it was read at, from one read transaction."""
        with self._read() as connection:
            ids = [row['id'] for row in connection.execute('SELECT id FROM jobs WHERE removed_at IS NULL ORDER BY seq')]
            jobs = tuple(self._job_record(connection, job_id) for job_id in ids)
            return Snapshot(jobs, self._read_meta(connection, 'revision'))

    def failures_snapshot(self) -> Snapshot:
        """Return every uncleared failure, oldest first, with the revision they were read at."""
        with self._read() as connection:
            rows = connection.execute('SELECT * FROM failures WHERE removed_at IS NULL ORDER BY seq')
            failures = tuple(_failure_record(row) for row in rows)
            return Snapshot(failures, self._read_meta(connection, 'revision'))

    def job_view(self, job_id: str) -> JobView | None:
        """Return a live job with its items and the view's revision, or None when unknown or removed."""
        with self._read() as connection:
            row = connection.execute(
                'SELECT revision FROM jobs WHERE id = ? AND removed_at IS NULL', (job_id,),
            ).fetchone()
            if row is None:
                return None
            items = connection.execute('SELECT MAX(revision) FROM items WHERE job_id = ?', (job_id,)).fetchone()[0]
            return JobView(self._job_record(connection, job_id), max(row['revision'], items or 0))

    def changes(self, since: int, limit: int) -> ChangePage:
        """Return the changes after ``since``: each changed record once, latest state, by revision.

        A record that changes again while a caller pages moves to a later page, so following the
        cursors until ``more`` is false yields the latest state of everything that changed (C38).

        Args:
            since: The cursor: the revision the caller has seen up to.
            limit: The most entries to return; at least 1.

        Raises:
            CursorPruned: If ``since`` is below the pruned-through revision (decision C27).
            ValueError: If ``limit`` is less than 1.
        """
        if limit < 1:
            raise ValueError(f'limit must be at least 1, not {limit}')
        with self._read() as connection:
            pruned_through = self._read_meta(connection, 'pruned_through')
            if since < pruned_through:
                raise CursorPruned(pruned_through)
            entries: list[Record] = []
            for kind, table, convert in (
                ('job', 'jobs', _job_fields), ('item', 'items', _item_record), ('failure', 'failures', _failure_record),
            ):
                rows = connection.execute(
                    f'SELECT * FROM {table} WHERE revision > ? ORDER BY revision LIMIT ?', (since, limit + 1),
                )
                for row in rows:
                    record = convert(row)
                    entries.append({'kind': kind, 'id': row['id'], 'revision': row['revision'],
                                    'removed': row['removed_at'] is not None, 'record': record})
        entries.sort(key=_revision_of)
        page = tuple(entries[:limit])
        return ChangePage(page, _revision_of(page[-1]) if page else since, more=len(entries) > limit)

    @staticmethod
    def _read_meta(connection: sqlite3.Connection, key: str) -> int:
        """Read one counter from the meta table on the given connection."""
        value: int = connection.execute('SELECT value FROM meta WHERE key = ?', (key,)).fetchone()[0]
        return value

    def _job_record(self, connection: sqlite3.Connection, job_id: str) -> Record:
        """Build a job's representation with its items not removed."""
        row = connection.execute('SELECT * FROM jobs WHERE id = ?', (job_id,)).fetchone()
        items = connection.execute(
            'SELECT * FROM items WHERE job_id = ? AND removed_at IS NULL ORDER BY position', (job_id,),
        )
        return {**_job_fields(row), 'items': [_item_record(item) for item in items]}

    # Worker writes

    def next_work(self, after_seq: int) -> WorkUnit | None:
        """Return the next unit of work in round-robin order (decision C9).

        Jobs are visited in submission order, starting after ``after_seq`` and wrapping around. A
        job has work when it is a queued playlist job, which needs expansion, or has a queued item;
        the item returned is its first queued one in playlist order.

        Args:
            after_seq: The ``job_seq`` of the unit last taken, or 0.

        Returns:
            The unit, or None when no job has work.
        """
        with self._read() as connection:
            job = connection.execute(
                "SELECT * FROM jobs AS j WHERE removed_at IS NULL AND state != 'finished' AND ("
                " (state = 'queued' AND target_type = 'playlist')"
                " OR (state != 'expanding' AND EXISTS (SELECT 1 FROM items AS i"
                "     WHERE i.job_id = j.id AND i.state = 'queued' AND i.removed_at IS NULL)))"
                ' ORDER BY seq <= ?, seq LIMIT 1',
                (after_seq,),
            ).fetchone()
            if job is None:
                return None
            item = None
            if not (job['state'] == JobState.QUEUED and job['target_type'] == TargetType.PLAYLIST):
                row = connection.execute(
                    "SELECT id, video_id, title FROM items WHERE job_id = ? AND state = 'queued' AND removed_at IS NULL"
                    ' ORDER BY position LIMIT 1',
                    (job['id'],),
                ).fetchone()
                item = WorkItem(row['id'], row['video_id'], row['title'])
            languages = job['languages']
            return WorkUnit(
                job_seq=job['seq'], job_id=job['id'], kind=JobKind(job['kind']),
                target_type=TargetType(job['target_type']),
                target_id=job['target_id'], languages=None if languages is None else tuple(json.loads(languages)),
                playlist_title=job['playlist_title'], item=item,
            )

    def start_expansion(self, job_id: str) -> None:
        """Mark a queued playlist job as expanding."""
        with self._write() as connection:
            self._update_job(connection, job_id, state=JobState.EXPANDING)

    def complete_expansion(self, job_id: str, playlist_title: str, entries: Sequence[PlaylistEntry]) -> None:
        """Persist a playlist's entries as queued items and start the job, in one transaction.

        The job becomes ``running``, or ``finished`` when the playlist had no entries. Uncleared
        expansion failures for the same playlist are superseded (DESIGN 5.9).
        """
        with self._write() as connection:
            now = self._clock()
            job = connection.execute('SELECT kind, target_id FROM jobs WHERE id = ?', (job_id,)).fetchone()
            kind = JobKind(job['kind'])
            for position, entry in enumerate(entries):
                self._insert_item(connection, job_id, kind, position, entry, now)
            state = JobState.RUNNING if entries else JobState.FINISHED
            self._update_job(connection, job_id, state=state, playlist_title=playlist_title)
            self._supersede(connection, "artifact = 'playlist' AND playlist_id = ?", (job['target_id'],), now)

    def fail_expansion(self, job_id: str, failure: NewFailure) -> None:
        """Finish a playlist job whose expansion failed, recording the failure (DESIGN 5.7)."""
        with self._write() as connection:
            self._update_job(connection, job_id, state=JobState.FINISHED)
            self._insert_failure(connection, job_id, None, failure)

    def start_item(self, item_id: str) -> None:
        """Mark an item as running, and its job as running if it was queued."""
        with self._write() as connection:
            now = self._clock()
            job_id = self._update_item(connection, item_id, now, state=ItemState.RUNNING)
            if self._job_state(connection, job_id) is JobState.QUEUED:
                self._update_job(connection, job_id, state=JobState.RUNNING)

    def record_progress(self, item_id: str, downloaded_bytes: int, total_bytes: int | None) -> None:
        """Record a running item's download progress; throttling is the caller's job."""
        with self._write() as connection:
            self._update_item(
                connection, item_id, self._clock(), downloaded_bytes=downloaded_bytes, total_bytes=total_bytes,
            )

    def finish_item(self, item_id: str, result: ItemResult) -> None:
        """Record how an item ended, with its failures and their effects, in one transaction.

        Definitive outcomes supersede earlier uncleared failures for the same video and artifact
        (DESIGN 5.9). The job finishes once none of its items is queued or running.

        Raises:
            ValueError: If ``result.state`` is not ``DONE`` or ``FAILED``.
        """
        if result.state not in (ItemState.DONE, ItemState.FAILED):
            raise ValueError(f'An item finishes as done or failed, not {result.state}')
        with self._write() as connection:
            now = self._clock()
            item = connection.execute('SELECT job_id, video_id, title FROM items WHERE id = ?', (item_id,)).fetchone()
            self._update_item(
                connection, item_id, now, state=result.state,
                title=result.title if result.title is not None else item['title'],
                media_outcome=result.media_outcome, media_path=result.media_path,
                captions_outcome=result.captions_outcome, captions_path=result.captions_path,
                track_code=result.track_code, track_kind=result.track_kind, downloaded_bytes=None, total_bytes=None,
            )
            for failure in result.failures:
                self._insert_failure(connection, item['job_id'], item_id, failure)
            for artifact, definitive in (
                (_Artifact.MEDIA, result.media_outcome in _DEFINITIVE_MEDIA),
                (_Artifact.CAPTIONS, result.captions_outcome in _DEFINITIVE_CAPTIONS),
            ):
                if definitive:
                    self._supersede(
                        connection, 'artifact = ? AND video_id = ? AND (item_id IS NULL OR item_id != ?)',
                        (artifact, item['video_id'], item_id), now,
                    )
            unfinished = connection.execute(
                "SELECT 1 FROM items WHERE job_id = ? AND state IN ('queued', 'running') AND removed_at IS NULL",
                (item['job_id'],),
            ).fetchone()
            if unfinished is None:
                self._update_job(connection, item['job_id'], state=JobState.FINISHED)

    # Reconciliation writes

    def finished_items(self) -> list[ItemFiles]:
        """Return every done or failed item in the status table with its recorded files."""
        rows = self._connection.execute(
            "SELECT id, job_id, media_path, captions_path FROM items WHERE state IN ('done', 'failed')"
            ' AND removed_at IS NULL ORDER BY job_id, position',
        )
        return [
            ItemFiles(row['id'], row['job_id'], tuple(p for p in (row['media_path'], row['captions_path']) if p))
            for row in rows
        ]

    def remove_items(self, item_ids: Sequence[str]) -> int:
        """Remove done or failed items, leaving tombstones; others are left alone.

        Returns:
            How many items were removed.
        """
        removed = 0
        with self._write() as connection:
            now = self._clock()
            for item_id in item_ids:
                cursor = connection.execute(
                    "UPDATE items SET removed_at = ?, updated_at = ?, revision = ? WHERE id = ?"
                    " AND state IN ('done', 'failed') AND removed_at IS NULL",
                    (now, now, self._next_revision(connection), item_id),
                )
                removed += cursor.rowcount
        return removed

    def remove_finished_jobs(self) -> int:
        """Remove every finished job with no items left and no uncleared failure (decision C26).

        Removing a job stores its final representation with its idempotency key, which then expires
        ``KEY_RETENTION_SECONDS`` later (decisions C24 and C34).

        Returns:
            How many jobs were removed.
        """
        with self._write() as connection:
            now = self._clock()
            ids = [row['id'] for row in connection.execute(
                "SELECT id FROM jobs AS j WHERE state = 'finished' AND removed_at IS NULL"
                ' AND NOT EXISTS (SELECT 1 FROM items AS i WHERE i.job_id = j.id AND i.removed_at IS NULL)'
                ' AND NOT EXISTS (SELECT 1 FROM failures AS f WHERE f.job_id = j.id AND f.removed_at IS NULL)'
                ' ORDER BY seq',
            )]
            for job_id in ids:
                connection.execute(
                    'UPDATE jobs SET removed_at = ?, updated_at = ?, revision = ? WHERE id = ?',
                    (now, now, self._next_revision(connection), job_id),
                )
                connection.execute(
                    'UPDATE idempotency_keys SET final = ?, expires_at = ? WHERE job_id = ?',
                    (json.dumps(self._job_record(connection, job_id)), now + KEY_RETENTION_SECONDS, job_id),
                )
            return len(ids)

    # Failures

    def clear_failures(self) -> list[Record]:
        """Clear every uncleared failure and return the cleared records."""
        with self._write() as connection:
            ids = [row['id'] for row in connection.execute(
                'SELECT id FROM failures WHERE removed_at IS NULL ORDER BY seq',
            )]
            return [self._remove_failure(connection, failure_id, 'cleared', self._clock()) for failure_id in ids]

    def clear_failure(self, failure_id: str) -> Record | None:
        """Clear one failure and return its record, or None when it is unknown or already cleared."""
        with self._write() as connection:
            row = connection.execute(
                'SELECT id FROM failures WHERE id = ? AND removed_at IS NULL', (failure_id,),
            ).fetchone()
            return None if row is None else self._remove_failure(connection, failure_id, 'cleared', self._clock())

    # Maintenance

    def prune(self, retention_seconds: float) -> int:
        """Delete tombstones older than the retention and expired idempotency keys.

        The durable pruned-through revision rises to the highest revision deleted, so a change-feed
        cursor below it is known to have missed something (decision C27).

        Returns:
            How many tombstones were deleted.
        """
        with self._write() as connection:
            now = self._clock()
            cutoff = now - retention_seconds
            deleted = 0
            highest = 0
            for table in ('jobs', 'items', 'failures'):
                rows = connection.execute(
                    f'DELETE FROM {table} WHERE removed_at IS NOT NULL AND removed_at <= ? RETURNING revision',
                    (cutoff,),
                ).fetchall()
                deleted += len(rows)
                highest = max([highest, *(row[0] for row in rows)])
            connection.execute(
                "UPDATE meta SET value = MAX(value, ?) WHERE key = 'pruned_through'", (highest,),
            )
            connection.execute('DELETE FROM idempotency_keys WHERE expires_at <= ?', (now,))
            return deleted

    def recover(self) -> None:
        """Requeue work interrupted by a restart (DESIGN 5.12).

        Running items return to queued, and jobs left expanding return to queued so they expand
        again; each change takes a revision.
        """
        with self._write() as connection:
            now = self._clock()
            for row in connection.execute("SELECT id FROM items WHERE state = 'running' ORDER BY rowid").fetchall():
                self._update_item(
                    connection, row['id'], now, state=ItemState.QUEUED, downloaded_bytes=None, total_bytes=None,
                )
            for row in connection.execute("SELECT id FROM jobs WHERE state = 'expanding' ORDER BY seq").fetchall():
                self._update_job(connection, row['id'], state=JobState.QUEUED)

    # Row helpers

    def _update_job(self, connection: sqlite3.Connection, job_id: str, **fields: object) -> None:
        """Set job columns and take a revision."""
        self._update(connection, 'jobs', 'id', job_id, self._clock(), fields)

    def _update_item(self, connection: sqlite3.Connection, item_id: str, now: float, **fields: object) -> str:
        """Set item columns, take a revision and return the item's job ID."""
        self._update(connection, 'items', 'id', item_id, now, fields)
        job_id: str = connection.execute('SELECT job_id FROM items WHERE id = ?', (item_id,)).fetchone()[0]
        return job_id

    def _update(
        self, connection: sqlite3.Connection, table: str, key: str, value: str, now: float, fields: dict[str, object],
    ) -> None:
        """Set columns of one row by key, stamping ``updated_at`` and the next revision.

        Raises:
            LookupError: If no row has that key.
        """
        assignments = ', '.join(f'{column} = ?' for column in fields)
        cursor = connection.execute(
            f'UPDATE {table} SET {assignments}, updated_at = ?, revision = ? WHERE {key} = ?',
            (*fields.values(), now, self._next_revision(connection), value),
        )
        if cursor.rowcount != 1:
            raise LookupError(f'No {table} row with {key} {value!r}')

    @staticmethod
    def _job_state(connection: sqlite3.Connection, job_id: str) -> JobState:
        """Return a job's state."""
        return JobState(connection.execute('SELECT state FROM jobs WHERE id = ?', (job_id,)).fetchone()[0])

    def _insert_failure(
        self, connection: sqlite3.Connection, job_id: str, item_id: str | None, failure: NewFailure,
    ) -> None:
        """Insert a failure with its job's and item's context and the next revision."""
        now = self._clock()
        job = connection.execute(
            'SELECT kind, url, target_type, target_id, playlist_title FROM jobs WHERE id = ?', (job_id,),
        ).fetchone()
        kind = JobKind(job['kind'])
        video_id = None
        if item_id is not None:
            video_id = connection.execute('SELECT video_id FROM items WHERE id = ?', (item_id,)).fetchone()[0]
        playlist = job['target_type'] == TargetType.PLAYLIST
        connection.execute(
            'INSERT INTO failures (id, job_id, job_kind, item_id, url, video_id, playlist_id, playlist_title, phase,'
            ' artifact, category, message, created_at, updated_at, revision)'
            ' VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (uuid.uuid4().hex, job_id, kind, item_id, job['url'], video_id,
             job['target_id'] if playlist else None, job['playlist_title'] if playlist else None,
             failure.phase, _artifact_of(failure.phase, kind), failure.category, failure.message, now, now,
             self._next_revision(connection)),
        )

    def _supersede(
        self, connection: sqlite3.Connection, condition: str, parameters: tuple[object, ...], now: float,
    ) -> None:
        """Remove uncleared failures matching ``condition`` as superseded."""
        ids = [row['id'] for row in connection.execute(
            f'SELECT id FROM failures WHERE removed_at IS NULL AND {condition} ORDER BY seq', parameters,
        )]
        for failure_id in ids:
            self._remove_failure(connection, failure_id, 'superseded', now)

    def _remove_failure(self, connection: sqlite3.Connection, failure_id: str, removal: str, now: float) -> Record:
        """Turn a failure into a tombstone with the next revision and return its final record."""
        connection.execute(
            'UPDATE failures SET removed_at = ?, removal = ?, updated_at = ?, revision = ? WHERE id = ?',
            (now, removal, now, self._next_revision(connection), failure_id),
        )
        return _failure_record(connection.execute('SELECT * FROM failures WHERE id = ?', (failure_id,)).fetchone())


def _artifact_of(phase: Phase, kind: JobKind) -> _Artifact:
    """Return what a failure in ``phase`` is about, which decides what supersedes it.

    A metadata failure belongs to media when the job requests media and to captions otherwise
    (assumption recorded in the T19 boundary).
    """
    match phase:
        case Phase.EXPANDING:
            return _Artifact.PLAYLIST
        case Phase.MEDIA:
            return _Artifact.MEDIA
        case Phase.CAPTIONS:
            return _Artifact.CAPTIONS
    return _Artifact.MEDIA if kind.wants_media else _Artifact.CAPTIONS


def _job_fields(row: sqlite3.Row) -> Record:
    """Return a job's representation without its items."""
    languages = row['languages']
    return {
        'id': row['id'],
        'kind': row['kind'],
        'url': row['url'],
        'target': {'type': row['target_type'], 'id': row['target_id']},
        'languages': None if languages is None else json.loads(languages),
        'state': row['state'],
        'playlist_title': row['playlist_title'],
        'created_at': _iso(row['created_at']),
        'updated_at': _iso(row['updated_at']),
        'revision': row['revision'],
        'removed': row['removed_at'] is not None,
    }


def _item_record(row: sqlite3.Row) -> Record:
    """Return an item's representation; only the artifacts its job requests appear."""
    kind = JobKind(row['job_kind'])
    downloaded, total = row['downloaded_bytes'], row['total_bytes']
    progress = None
    if downloaded is not None:
        percent = None if not total else round(100.0 * downloaded / total, 1)
        progress = {'downloaded_bytes': downloaded, 'total_bytes': total, 'percent': percent}
    record: Record = {
        'id': row['id'],
        'job_id': row['job_id'],
        'position': row['position'],
        'video_id': row['video_id'],
        'title': row['title'],
        'state': row['state'],
        'progress': progress,
    }
    if kind.wants_media:
        record['media'] = {'outcome': row['media_outcome'], 'path': row['media_path']}
    if kind.wants_captions:
        track = None if row['track_code'] is None else {'code': row['track_code'], 'kind': row['track_kind']}
        record['captions'] = {'outcome': row['captions_outcome'], 'path': row['captions_path'], 'track': track}
    record.update(created_at=_iso(row['created_at']), updated_at=_iso(row['updated_at']), revision=row['revision'],
                  removed=row['removed_at'] is not None)
    return record


def _failure_record(row: sqlite3.Row) -> Record:
    """Return a failure's representation; ``removal`` says whether it was cleared or superseded."""
    return {
        'id': row['id'],
        'created_at': _iso(row['created_at']),
        'job_id': row['job_id'],
        'job_kind': row['job_kind'],
        'item_id': row['item_id'],
        'url': row['url'],
        'video_id': row['video_id'],
        'playlist_id': row['playlist_id'],
        'playlist_title': row['playlist_title'],
        'phase': row['phase'],
        'category': row['category'],
        'message': row['message'],
        'revision': row['revision'],
        'removed': row['removed_at'] is not None,
        'removal': row['removal'],
    }


def _revision_of(entry: Record) -> int:
    """Return a change entry's revision."""
    revision = entry['revision']
    assert isinstance(revision, int)  # set from an INTEGER column by Store.changes
    return revision
