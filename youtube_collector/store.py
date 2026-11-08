"""The SQLite store: jobs, their items, and failures.

SQLite is the source of truth (DESIGN 5.10). Every write is one short ``BEGIN IMMEDIATE``
transaction (decision C36). Finished jobs are kept for a retention period, whatever has happened to
their files, and are deleted when a later submission finds them expired (decisions C40 and C42).
Failures are deleted when they are cleared or superseded, and outlive the job they came from.

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
    'SCHEMA_VERSION', 'CaptionsOutcome', 'ItemResult', 'ItemState', 'JobKind', 'JobState', 'MediaOutcome',
    'NewFailure', 'Phase', 'PlaylistEntry', 'Store', 'TargetType', 'TrackKind', 'WorkItem', 'WorkUnit', 'initialize',
]

SCHEMA_VERSION = 2
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


_SCHEMA = """
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
    finished_at REAL
) STRICT;
CREATE INDEX jobs_by_finished_at ON jobs (finished_at);
CREATE TABLE items (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
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
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
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
    created_at REAL NOT NULL
) STRICT;
CREATE INDEX failures_by_video ON failures (video_id, artifact);
CREATE INDEX failures_by_job ON failures (job_id);
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

    # Transactions

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

    def check(self) -> None:
        """Check that the database is readable and writable, for ``/health`` (DESIGN 5.14).

        Takes the write lock with ``BEGIN IMMEDIATE``, reads a row, and commits nothing.

        Raises:
            sqlite3.Error: If the database cannot be read or locked for writing.
        """
        with self._write() as connection:
            connection.execute('SELECT 1 FROM jobs LIMIT 1').fetchone()

    # Submission

    def submit(
        self, *, kind: JobKind, url: str, target_type: TargetType, target_id: str,
        languages: Sequence[str] | None, retention_seconds: float,
    ) -> Record:
        """Create a job and return its representation, first deleting jobs past their retention.

        A single-video job gets its one item at once; a playlist job gets its items on expansion.
        Only submissions add records, so deleting expired jobs here bounds the table without a
        background task (decision C42). An unfinished job has no ``finished_at`` and never expires.
        Failures are kept: they stay until cleared or superseded (DESIGN 5.8).

        Args:
            kind: The job kind.
            url: The URL as submitted.
            target_type: Whether the URL names a video or a playlist.
            target_id: The video or playlist ID.
            languages: Caption language candidates, or None for a ``videos`` job.
            retention_seconds: How long a finished job stays readable.

        Returns:
            The new job, with its items.
        """
        with self._write() as connection:
            now = self._clock()
            cutoff = now - retention_seconds
            connection.execute(
                'DELETE FROM items WHERE job_id IN (SELECT id FROM jobs WHERE finished_at <= ?)', (cutoff,),
            )
            connection.execute('DELETE FROM jobs WHERE finished_at <= ?', (cutoff,))
            job_id = uuid.uuid4().hex
            connection.execute(
                'INSERT INTO jobs (id, kind, url, target_type, target_id, languages, state, created_at, updated_at)'
                ' VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (job_id, kind, url, target_type, target_id, None if languages is None else json.dumps(list(languages)),
                 JobState.QUEUED, now, now),
            )
            if target_type is TargetType.VIDEO:
                self._insert_item(connection, job_id, 0, PlaylistEntry(target_id), now)
            return self._job_record(connection, job_id)

    @staticmethod
    def _insert_item(
        connection: sqlite3.Connection, job_id: str, position: int, entry: PlaylistEntry, now: float,
    ) -> None:
        """Insert one queued item."""
        connection.execute(
            'INSERT INTO items (id, job_id, position, video_id, title, state, created_at, updated_at)'
            ' VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (uuid.uuid4().hex, job_id, position, entry.video_id, entry.title, ItemState.QUEUED, now, now),
        )

    # Reads

    def get_job(self, job_id: str) -> Record | None:
        """Return a job with its items and uncleared failures, or None when it is unknown or deleted."""
        with self._read() as connection:
            row = connection.execute('SELECT id FROM jobs WHERE id = ?', (job_id,)).fetchone()
            return None if row is None else self._job_record(connection, job_id)

    def list_jobs(self) -> list[Record]:
        """Return the status table: every retained job, in submission order."""
        with self._read() as connection:
            ids = [row['id'] for row in connection.execute('SELECT id FROM jobs ORDER BY seq')]
            return [self._job_record(connection, job_id) for job_id in ids]

    def list_failures(self) -> list[Record]:
        """Return every uncleared failure, oldest first."""
        return [_failure_record(row) for row in self._connection.execute('SELECT * FROM failures ORDER BY seq')]

    @staticmethod
    def _job_record(connection: sqlite3.Connection, job_id: str) -> Record:
        """Build a job's representation with its items and uncleared failures (decision C39)."""
        row = connection.execute('SELECT * FROM jobs WHERE id = ?', (job_id,)).fetchone()
        kind = JobKind(row['kind'])
        items = connection.execute('SELECT * FROM items WHERE job_id = ? ORDER BY position', (job_id,))
        failures = connection.execute('SELECT * FROM failures WHERE job_id = ? ORDER BY seq', (job_id,))
        return {
            **_job_fields(row), 'items': [_item_record(item, kind) for item in items],
            'failures': [_failure_record(failure) for failure in failures],
        }

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
                "SELECT * FROM jobs AS j WHERE state != 'finished' AND ("
                " (state = 'queued' AND target_type = 'playlist')"
                " OR (state != 'expanding' AND EXISTS (SELECT 1 FROM items AS i"
                "     WHERE i.job_id = j.id AND i.state = 'queued')))"
                ' ORDER BY seq <= ?, seq LIMIT 1',
                (after_seq,),
            ).fetchone()
            if job is None:
                return None
            item = None
            if not (job['state'] == JobState.QUEUED and job['target_type'] == TargetType.PLAYLIST):
                row = connection.execute(
                    "SELECT id, video_id, title FROM items WHERE job_id = ? AND state = 'queued'"
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
            job = connection.execute('SELECT target_id FROM jobs WHERE id = ?', (job_id,)).fetchone()
            for position, entry in enumerate(entries):
                self._insert_item(connection, job_id, position, entry, now)
            state = JobState.RUNNING if entries else JobState.FINISHED
            self._update_job(connection, job_id, state=state, playlist_title=playlist_title)
            self._supersede(connection, "artifact = 'playlist' AND playlist_id = ?", (job['target_id'],))

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
                track_code=result.track_code, track_kind=result.track_kind,
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
                        (artifact, item['video_id'], item_id),
                    )
            unfinished = connection.execute(
                "SELECT 1 FROM items WHERE job_id = ? AND state IN ('queued', 'running')",
                (item['job_id'],),
            ).fetchone()
            if unfinished is None:
                self._update_job(connection, item['job_id'], state=JobState.FINISHED)

    # Failures

    def clear_failures(self) -> list[Record]:
        """Delete every uncleared failure and return the cleared records, oldest first."""
        with self._write() as connection:
            rows = connection.execute('SELECT * FROM failures ORDER BY seq').fetchall()
            connection.execute('DELETE FROM failures')
            return [_failure_record(row) for row in rows]

    def clear_failure(self, failure_id: str) -> Record | None:
        """Delete one failure and return its record, or None when it is unknown or already cleared."""
        with self._write() as connection:
            row = connection.execute('DELETE FROM failures WHERE id = ? RETURNING *', (failure_id,)).fetchone()
            return None if row is None else _failure_record(row)

    # Maintenance

    def recover(self) -> None:
        """Requeue work interrupted by a restart (DESIGN 5.12).

        Running items return to queued, and jobs left expanding return to queued so they expand
        again.
        """
        with self._write() as connection:
            now = self._clock()
            for row in connection.execute("SELECT id FROM items WHERE state = 'running' ORDER BY rowid").fetchall():
                self._update_item(connection, row['id'], now, state=ItemState.QUEUED)
            for row in connection.execute("SELECT id FROM jobs WHERE state = 'expanding' ORDER BY seq").fetchall():
                self._update_job(connection, row['id'], state=JobState.QUEUED)

    # Row helpers

    def _update_job(self, connection: sqlite3.Connection, job_id: str, **fields: object) -> None:
        """Set job columns, stamping ``finished_at`` when the job finishes, which starts its retention."""
        now = self._clock()
        if fields.get('state') is JobState.FINISHED:
            fields['finished_at'] = now
        self._update(connection, 'jobs', 'id', job_id, now, fields)

    def _update_item(self, connection: sqlite3.Connection, item_id: str, now: float, **fields: object) -> str:
        """Set item columns and return the item's job ID."""
        self._update(connection, 'items', 'id', item_id, now, fields)
        job_id: str = connection.execute('SELECT job_id FROM items WHERE id = ?', (item_id,)).fetchone()[0]
        return job_id

    @staticmethod
    def _update(
        connection: sqlite3.Connection, table: str, key: str, value: str, now: float, fields: dict[str, object],
    ) -> None:
        """Set columns of one row by key, stamping ``updated_at``.

        Raises:
            LookupError: If no row has that key.
        """
        assignments = ', '.join(f'{column} = ?' for column in fields)
        cursor = connection.execute(
            f'UPDATE {table} SET {assignments}, updated_at = ? WHERE {key} = ?', (*fields.values(), now, value),
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
        """Insert a failure with its job's and item's context."""
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
            ' artifact, category, message, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (uuid.uuid4().hex, job_id, kind, item_id, job['url'], video_id,
             job['target_id'] if playlist else None, job['playlist_title'] if playlist else None,
             failure.phase, _artifact_of(failure.phase, kind), failure.category, failure.message, now),
        )

    @staticmethod
    def _supersede(connection: sqlite3.Connection, condition: str, parameters: tuple[object, ...]) -> None:
        """Delete the uncleared failures matching ``condition``: a later attempt settled them (DESIGN 5.9)."""
        connection.execute(f'DELETE FROM failures WHERE {condition}', parameters)


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
    }


def _item_record(row: sqlite3.Row, kind: JobKind) -> Record:
    """Return an item's representation; only the artifacts its job's ``kind`` requests appear."""
    record: Record = {
        'id': row['id'],
        'job_id': row['job_id'],
        'position': row['position'],
        'video_id': row['video_id'],
        'title': row['title'],
        'state': row['state'],
    }
    if kind.wants_media:
        record['media'] = {'outcome': row['media_outcome'], 'path': row['media_path']}
    if kind.wants_captions:
        track = None if row['track_code'] is None else {'code': row['track_code'], 'kind': row['track_kind']}
        record['captions'] = {'outcome': row['captions_outcome'], 'path': row['captions_path'], 'track': track}
    record.update(created_at=_iso(row['created_at']), updated_at=_iso(row['updated_at']))
    return record


def _failure_record(row: sqlite3.Row) -> Record:
    """Return a failure's representation."""
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
    }
