"""The HTTP API and the service's composition (DESIGN 5.1, 5.8, 5.10, 5.14 and section 6).

One process runs the HTTP server and the YouTube worker thread (DESIGN section 7). Request handlers
never contact YouTube and never touch the staging share (decision C25): the worker owns both, and
handlers read the database and the worker's cached staging check. Database calls run on one dedicated
thread with its own connection, so the event loop never blocks on SQLite.
"""

import asyncio
import logging
import sqlite3
import threading
import time
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Self

import yt_dlp.version
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from youtube_collector.captions import CaptionClient
from youtube_collector.config import Settings
from youtube_collector.languages import is_well_formed
from youtube_collector.responses import FailureList, FailureResponse, Health, JobList, JobResponse
from youtube_collector.store import JobKind, Store, TargetType, initialize
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
    """Everything the service starts: the worker thread and the API's database thread."""

    def __init__(self, settings: Settings, components: Components) -> None:
        """Create the database if needed and build the components; nothing runs until ``start``."""
        self.settings = settings
        self.components = components
        initialize(settings.database)
        self.worker = Worker(
            open_store=self._open_store, staging_root=settings.staging_dir, media=components.media,
            captions=components.captions, monotonic=components.monotonic,
        )
        self.worker_thread = threading.Thread(target=self.worker.run, name='youtube-worker', daemon=True)
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

    def start(self) -> None:
        """Start the worker thread."""
        self.worker_thread.start()

    def stop(self) -> None:
        """Ask the worker to stop, wait up to the shutdown grace for it, then close the API's database thread.

        The grace bounds waiting, not execution: a worker still busy when it ends is abandoned and keeps
        running until the process exits, and restart recovery requeues its item (decisions C36 and C50).
        Closing the database thread waits without a bound for any operation running on it, so a stalled
        operation can keep the process alive until the container's forced termination (decision C50).
        """
        self.worker.stop()
        self.worker_thread.join(self.settings.shutdown_grace)
        if self.worker_thread.is_alive():
            logger.warning('%s did not stop within the shutdown grace; abandoning it', self.worker_thread.name)
        try:
            self._executor.submit(self._close_api_store).result()
        finally:
            self._executor.shutdown(wait=True)


def create_app(
    settings: Settings, components_factory: Callable[[Settings], Components] = default_components,
) -> FastAPI:
    """Build the FastAPI application; its lifespan starts and stops the worker thread.

    Args:
        settings: The service settings.
        components_factory: Builds the adapters and clocks at startup; the default detects cookies.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        runtime = _Runtime(settings, components_factory(settings))
        runtime.start()
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

    async def submit(rt: _Runtime, kind: JobKind, body: _VideosRequest) -> Body:
        """Create a job and wake the worker (DESIGN 5.1); expired jobs are deleted in the same write."""
        target = parse_target(body.url)
        languages = body.languages if isinstance(body, _CaptionsRequest) else None
        target_type = TargetType.PLAYLIST if isinstance(target, PlaylistTarget) else TargetType.VIDEO
        target_id = target.playlist_id if isinstance(target, PlaylistTarget) else target.video_id
        job = await rt.db(lambda store: store.submit(
            kind=kind, url=body.url, target_type=target_type, target_id=target_id, languages=languages,
            retention_seconds=rt.settings.job_retention,
        ))
        rt.worker.wake()
        return job

    # Throughout: exclude_unset keeps absent fields absent and null ones null, because an item carries
    # only the artifacts its job requests.
    @app.post('/jobs/videos', status_code=status.HTTP_202_ACCEPTED, summary='Submit a media job',
              response_model=JobResponse, response_model_exclude_unset=True)
    async def submit_videos(body: _VideosRequest, rt: service) -> Body:
        """Download MP4 media (DESIGN 5.1)."""
        return await submit(rt, JobKind.VIDEOS, body)

    @app.post('/jobs/captions', status_code=status.HTTP_202_ACCEPTED, summary='Submit a captions job',
              response_model=JobResponse, response_model_exclude_unset=True)
    async def submit_captions(body: _CaptionsRequest, rt: service) -> Body:
        """Download SRT captions (DESIGN 5.1)."""
        return await submit(rt, JobKind.CAPTIONS, body)

    @app.post('/jobs/videos-with-captions', status_code=status.HTTP_202_ACCEPTED,
              summary='Submit a media-and-captions job', response_model=JobResponse,
              response_model_exclude_unset=True)
    async def submit_videos_with_captions(body: _CaptionsRequest, rt: service) -> Body:
        """Download MP4 media and SRT captions (DESIGN 5.1)."""
        return await submit(rt, JobKind.VIDEOS_WITH_CAPTIONS, body)

    @app.get('/jobs', summary='The status table', response_model=JobList, response_model_exclude_unset=True)
    async def list_jobs(rt: service) -> Body:
        """Every retained job, with its items and uncleared failures (DESIGN 5.8)."""
        return {'jobs': await rt.db(lambda store: store.list_jobs())}

    @app.get('/jobs/{job_id}', summary='One job', response_model=JobResponse, response_model_exclude_unset=True)
    async def get_job(job_id: str, rt: service) -> Body:
        """One job with its items and uncleared failures; poll this to follow it (DESIGN 5.10).

        404 for an unknown job and for one past its retention.
        """
        job = await rt.db(lambda store: store.get_job(job_id))
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f'Unknown job {job_id}')
        return job

    @app.get('/failures', summary='Uncleared failures', response_model=FailureList)
    async def list_failures(rt: service) -> Body:
        """Every uncleared failure, oldest first: the place to check errored tasks (DESIGN 5.9)."""
        return {'failures': await rt.db(lambda store: store.list_failures())}

    @app.delete('/failures', summary='Clear all failures', response_model=FailureList)
    async def clear_failures(rt: service) -> Body:
        """Clear every failure and return the cleared records."""
        return {'failures': await rt.db(lambda store: store.clear_failures())}

    @app.delete('/failures/{failure_id}', summary='Clear one failure', response_model=FailureResponse)
    async def clear_failure(failure_id: str, rt: service) -> Body:
        """Clear one failure and return it; 404 when unknown or already cleared."""
        cleared = await rt.db(lambda store: store.clear_failure(failure_id))
        if cleared is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f'Unknown failure {failure_id}')
        return cleared

    @app.get(
        '/health', summary='Service health', response_model=Health,
        responses={503: {'model': Health, 'description': 'Unhealthy; same body.'}},
    )
    async def health(rt: service) -> JSONResponse:
        """Database, staging marker, worker liveness and yt-dlp version, without contacting YouTube (5.14).

        The staging result is the worker's cached check with its age, so this never touches the
        share (C25). 200 when healthy, 503 otherwise, with the same body.
        """
        database_error = None
        try:
            await asyncio.wait_for(rt.db(lambda store: store.check()), 5)
        except (sqlite3.Error, TimeoutError) as error:
            database_error = str(error) or type(error).__name__
        check = rt.worker.staging_check()
        age = None if check is None else rt.components.monotonic() - check.checked_at
        staging_ok = check is not None and check.marker_present and age is not None \
            and age <= rt.settings.staging_health_max_age
        threads = {'worker': rt.worker_thread.is_alive()}
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
