"""The HTTP API and the service's composition (DESIGN 5.1, 5.8, 5.10, 5.14 and section 6).

One process runs the HTTP server, the YouTube worker thread and the reconciliation thread
(DESIGN section 7). Request handlers never contact YouTube and never touch the staging share
(decision C25): the worker owns YouTube, the reconciler owns staging, and handlers read the
database and the reconciler's cached results. Database calls run on one dedicated thread with
its own connection, so the event loop never blocks on SQLite; long polls wait on the loop.
"""

import asyncio
import json
import logging
import sqlite3
import threading
import time
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Self

import yt_dlp.version
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from youtube_collector.captions import CaptionClient
from youtube_collector.config import Settings
from youtube_collector.languages import is_well_formed
from youtube_collector.notify import Notifier
from youtube_collector.reconcile import Reconciler
from youtube_collector.store import CursorPruned, IdempotencyConflict, JobKind, Store, TargetType, initialize
from youtube_collector.urls import PlaylistTarget, UnsupportedUrlError, parse_target
from youtube_collector.worker import CaptionSource, MediaSource, Worker
from youtube_collector.youtube import YoutubeClient, firefox_cookies_present, firefox_profile_roots

__all__ = ['Components', 'create_app', 'default_components']

logger = logging.getLogger(__name__)

type Body = dict[str, object]


@dataclass
class Components:
    """The collaborators the service composes; tests substitute fakes for the adapters and clocks.

    Attributes:
        media: The yt-dlp adapter.
        captions: The caption adapter.
        clock: Wall-clock epoch seconds.
        monotonic: A monotonic clock.
    """

    media: MediaSource
    captions: CaptionSource
    clock: Callable[[], float] = field(default=time.time)
    monotonic: Callable[[], float] = field(default=time.monotonic)


def default_components(settings: Settings) -> Components:
    """Return the real adapters, detecting Firefox cookies once for the process (DESIGN 5.11)."""
    cookies = firefox_cookies_present(firefox_profile_roots(Path.home()))
    logger.info('Firefox cookies %s', 'found; yt-dlp will use them' if cookies else 'not found')
    return Components(media=YoutubeClient(cookies=cookies, pacing=settings.pacing), captions=CaptionClient())


class _VideosRequest(BaseModel):
    """The body of ``POST /jobs/videos``; ``languages`` is not accepted (DESIGN 5.1)."""

    model_config = ConfigDict(extra='forbid')

    url: str = Field(description='A single-video or playlist URL (DESIGN 5.2).')
    idempotency_key: str | None = Field(
        default=None, min_length=1, max_length=256,
        description='Returns the earlier job when reused with the same body; 409 with another body.',
    )

    @field_validator('url')
    @classmethod
    def _supported(cls, url: str) -> str:
        """Reject unsupported URLs with their reason; the check is syntactic only."""
        try:
            parse_target(url)
        except UnsupportedUrlError as error:
            raise ValueError(str(error)) from None
        return url


class _CaptionsRequest(_VideosRequest):
    """The body of the caption endpoints: ``languages`` is required (DESIGN 5.1)."""

    languages: list[str] = Field(
        min_length=1, description='Ordered caption language candidates, each a well-formed BCP 47 tag (DESIGN 5.6).',
    )

    @model_validator(mode='after')
    def _well_formed(self) -> Self:
        """Reject candidates that are not well-formed BCP 47 tags."""
        malformed = [tag for tag in self.languages if not is_well_formed(tag)]
        if malformed:
            raise ValueError(f'Not well-formed BCP 47 language tags: {malformed}')
        return self


class _Runtime:
    """Everything the service starts: the threads, the notifier and the API's database thread."""

    def __init__(self, settings: Settings, components: Components) -> None:
        """Create the database if needed and build the components; nothing runs until ``start``."""
        self.settings = settings
        self.components = components
        initialize(settings.database)
        # Long polls really wait on asyncio's clock, so their deadlines use the real monotonic clock too,
        # never the injected one, which tests freeze or advance by hand.
        self.notifier = Notifier()
        self.worker = Worker(
            open_store=self._open_store, staging_root=settings.staging_dir, media=components.media,
            captions=components.captions, notify=self.notifier.notify, monotonic=components.monotonic,
        )
        self.reconciler = Reconciler(
            open_store=self._open_store, staging_root=settings.staging_dir, notify=self.notifier.notify,
            clock=components.clock, monotonic=components.monotonic, interval=settings.reconcile_interval,
            tombstone_retention=settings.tombstone_retention,
        )
        self.worker_thread = threading.Thread(target=self.worker.run, name='youtube-worker', daemon=True)
        self.reconcile_thread = threading.Thread(target=self.reconciler.run, name='reconciler', daemon=True)
        self._local = threading.local()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='api-db')

    def _open_store(self) -> Store:
        """Open a store for the calling thread (decision C36)."""
        return Store(self.settings.database, self.components.clock)

    def _api_store(self) -> Store:
        """Return the API database thread's store, opening it on first use.

        Opening lazily, inside an operation, makes a failure to open an ordinary ``sqlite3.Error`` of
        that operation, which ``/health`` reports as 503, and lets a later call try again. An executor
        initializer would instead break the pool for good.
        """
        store: Store | None = getattr(self._local, 'store', None)
        if store is None:
            store = self._local.store = self._open_store()
        return store

    def _close_api_store(self) -> None:
        """Close the API database thread's store, if it was ever opened."""
        store: Store | None = getattr(self._local, 'store', None)
        if store is not None:
            store.close()
            self._local.store = None

    async def db[T](self, operation: Callable[[Store], T]) -> T:
        """Run a store operation on the API's database thread."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, lambda: operation(self._api_store()))

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind the notifier to the event loop and start both threads."""
        self.notifier.bind(loop)
        self.worker_thread.start()
        self.reconcile_thread.start()

    def stop(self) -> None:
        """Stop both threads within the shutdown grace, then close the API's database thread.

        The worker finishes its current unit; if a yt-dlp call outlasts the grace, it is abandoned
        and restart recovery requeues the item (decision C36).
        """
        self.worker.stop()
        self.reconciler.stop()
        deadline = time.monotonic() + self.settings.shutdown_grace
        for thread in (self.worker_thread, self.reconcile_thread):
            thread.join(max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                logger.warning('%s did not stop within the shutdown grace; abandoning it', thread.name)
        try:
            self._executor.submit(self._close_api_store).result()
        finally:
            self._executor.shutdown(wait=True)


def _iso(timestamp: float | None) -> str | None:
    """Format epoch seconds as ISO 8601 UTC, or pass None through."""
    return None if timestamp is None else datetime.fromtimestamp(timestamp, UTC).isoformat()


def create_app(
    settings: Settings, components_factory: Callable[[Settings], Components] = default_components,
) -> FastAPI:
    """Build the FastAPI application; its lifespan starts and stops the service's threads.

    Args:
        settings: The service settings.
        components_factory: Builds the adapters and clocks at startup; the default detects cookies.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        runtime = _Runtime(settings, components_factory(settings))
        runtime.start(asyncio.get_running_loop())
        app.state.runtime = runtime
        try:
            yield
        finally:
            await asyncio.to_thread(runtime.stop)

    app = FastAPI(
        title='youtube-collector', version='0.0.1', lifespan=lifespan,
        description='Downloads YouTube videos, playlists and captions into a staging area. LAN-only and '
                    'unauthenticated; see DESIGN.md.',
    )

    def runtime(request: Request) -> _Runtime:
        """Return the running service."""
        current: _Runtime = request.app.state.runtime
        return current

    service = Annotated[_Runtime, Depends(runtime)]

    async def submit(rt: _Runtime, kind: JobKind, body: _VideosRequest, response: Response) -> Body:
        """Create a job, or replay its idempotency key, and wake the worker (DESIGN 5.1)."""
        target = parse_target(body.url)
        languages = body.languages if isinstance(body, _CaptionsRequest) else None
        canonical = json.dumps({'kind': kind.value, 'url': body.url, 'languages': languages}, sort_keys=True)
        target_type = TargetType.PLAYLIST if isinstance(target, PlaylistTarget) else TargetType.VIDEO
        target_id = target.playlist_id if isinstance(target, PlaylistTarget) else target.video_id
        try:
            submission = await rt.db(lambda store: store.submit(
                kind=kind, url=body.url, target_type=target_type, target_id=target_id, languages=languages,
                idempotency_key=body.idempotency_key, body=canonical,
            ))
        except IdempotencyConflict as error:
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from None
        if submission.created:
            rt.notifier.notify()
            rt.worker.wake()
        else:
            response.status_code = status.HTTP_200_OK
        return submission.job

    @app.post('/jobs/videos', status_code=status.HTTP_202_ACCEPTED, summary='Submit a media job')
    async def submit_videos(body: _VideosRequest, response: Response, rt: service) -> Body:
        """Download MP4 media (DESIGN 5.1). 202 when created, 200 when an idempotency key replays."""
        return await submit(rt, JobKind.VIDEOS, body, response)

    @app.post('/jobs/captions', status_code=status.HTTP_202_ACCEPTED, summary='Submit a captions job')
    async def submit_captions(body: _CaptionsRequest, response: Response, rt: service) -> Body:
        """Download SRT captions (DESIGN 5.1). 202 when created, 200 when an idempotency key replays."""
        return await submit(rt, JobKind.CAPTIONS, body, response)

    @app.post('/jobs/videos-with-captions', status_code=status.HTTP_202_ACCEPTED,
              summary='Submit a media-and-captions job')
    async def submit_videos_with_captions(body: _CaptionsRequest, response: Response, rt: service) -> Body:
        """Download MP4 media and SRT captions (DESIGN 5.1). 202 when created, 200 on replay."""
        return await submit(rt, JobKind.VIDEOS_WITH_CAPTIONS, body, response)

    @app.get('/jobs', summary='The status table')
    async def list_jobs(rt: service) -> Body:
        """Every job with its items, after asking for a reconciliation pass (DESIGN 5.8, C38).

        Waits a bounded time for the pass, never touching the share itself (C25), and reports when
        the last pass completed and the revision the snapshot was read at.
        """
        ticket = rt.reconciler.request_pass()
        await asyncio.to_thread(rt.reconciler.wait_for_pass, ticket, rt.settings.jobs_reconcile_wait)
        snapshot = await rt.db(lambda store: store.jobs_snapshot())
        return {
            'jobs': list(snapshot.records), 'revision': snapshot.revision,
            'reconciled_at': _iso(rt.reconciler.last_reconciled_at()),
        }

    @app.get('/jobs/changes', summary='The change feed')
    async def changes(
        rt: service,
        since: Annotated[int, Query(ge=0, description='The cursor from the previous page; 0 to start.')] = 0,
        wait: Annotated[float, Query(ge=0, description='Seconds to wait for a change; capped.')] = 0,
        limit: Annotated[int, Query(ge=1, le=1000, description='The most entries to return.')] = 100,
    ) -> Body:
        """Changes after ``since``, each record once in its latest state, by revision (DESIGN 5.10, C38).

        Returns at once when changes exist, otherwise after the first change or ``wait`` seconds. 410
        means the cursor is older than pruned tombstones: resynchronize from ``GET /jobs`` and
        ``GET /failures`` (C27).
        """
        try:
            page = await rt.notifier.long_poll(
                lambda: rt.db(lambda store: store.changes(since, limit)), lambda page: bool(page.entries),
                min(wait, rt.settings.max_wait),
            )
        except CursorPruned as error:
            raise HTTPException(status.HTTP_410_GONE, str(error)) from None
        return {'changes': list(page.entries), 'cursor': page.cursor, 'more': page.more}

    @app.get('/jobs/{job_id}', summary='One job')
    async def get_job(
        job_id: str, rt: service,
        since: Annotated[int | None, Query(ge=0, description="Wait for the job's view revision to pass this.")] = None,
        wait: Annotated[float, Query(ge=0, description='Seconds to wait for a change; capped.')] = 0,
    ) -> Body:
        """One job with its items and ``view_revision``; with ``since``, waits for a change (DESIGN 5.10).

        404 once the job has left the status table; its tombstone reaches callers through the feed.
        """
        if since is None:
            view = await rt.db(lambda store: store.job_view(job_id))
        else:
            view = await rt.notifier.long_poll(
                lambda: rt.db(lambda store: store.job_view(job_id)),
                lambda current: current is None or current.revision > since, min(wait, rt.settings.max_wait),
            )
        if view is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f'Unknown job {job_id}')
        return {**view.job, 'view_revision': view.revision}

    @app.get('/failures', summary='Uncleared failures')
    async def list_failures(rt: service) -> Body:
        """Every uncleared failure, oldest first, with the revision they were read at (DESIGN 5.9, C38)."""
        snapshot = await rt.db(lambda store: store.failures_snapshot())
        return {'failures': list(snapshot.records), 'revision': snapshot.revision}

    @app.delete('/failures', summary='Clear all failures')
    async def clear_failures(rt: service) -> Body:
        """Clear every failure and return the cleared records."""
        cleared = await rt.db(lambda store: store.clear_failures())
        if cleared:
            rt.notifier.notify()
        return {'failures': cleared}

    @app.delete('/failures/{failure_id}', summary='Clear one failure')
    async def clear_failure(failure_id: str, rt: service) -> Body:
        """Clear one failure and return it; 404 when unknown or already cleared."""
        cleared = await rt.db(lambda store: store.clear_failure(failure_id))
        if cleared is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f'Unknown failure {failure_id}')
        rt.notifier.notify()
        return cleared

    @app.get('/health', summary='Service health', responses={503: {'description': 'Unhealthy; same body.'}})
    async def health(rt: service) -> JSONResponse:
        """Database, staging marker, thread liveness and yt-dlp version, without contacting YouTube (5.14).

        The staging result is the reconciler's cached check with its age, so this never touches the
        share (C25). 200 when healthy, 503 otherwise, with the same body.
        """
        database_error = None
        try:
            await asyncio.wait_for(rt.db(lambda store: store.check()), 5)
        except (sqlite3.Error, TimeoutError) as error:
            database_error = str(error) or type(error).__name__
        check = rt.reconciler.staging_check()
        age = None if check is None else rt.components.monotonic() - check.checked_at
        staging_ok = check is not None and check.marker_present and age is not None \
            and age <= rt.settings.staging_health_max_age
        threads = {'worker': rt.worker_thread.is_alive(), 'reconciler': rt.reconcile_thread.is_alive()}
        healthy = database_error is None and staging_ok and all(threads.values())
        body = {
            'healthy': healthy,
            'database': {'ok': database_error is None, 'error': database_error},
            'staging': {
                'ok': staging_ok, 'marker_present': None if check is None else check.marker_present,
                'checked_seconds_ago': None if age is None else round(age, 3),
                'paused': check is not None and not check.marker_present,
            },
            'threads': threads,
            'yt_dlp_version': yt_dlp.version.__version__,
        }
        return JSONResponse(body, status_code=status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE)

    return app
