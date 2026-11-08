"""The HTTP API's response models, which publish every field in the OpenAPI document (DESIGN section 6).

These describe what ``store`` and ``/health`` already return and do not reshape it. Extra fields are
forbidden, so a field added to a record without a model here fails loudly instead of going undocumented.
An item carries ``media`` and ``captions`` only for the artifacts its job requests, so routes serialize
with ``response_model_exclude_unset``: an absent field stays absent, and a null one stays null.
"""

from pydantic import BaseModel, ConfigDict, Field

from youtube_collector.categories import Category
from youtube_collector.store import (
    CaptionsOutcome,
    ItemState,
    JobKind,
    JobState,
    MediaOutcome,
    Phase,
    TargetType,
    TrackKind,
)

__all__ = ['FailureList', 'FailureResponse', 'Health', 'JobList', 'JobResponse']


class _Response(BaseModel):
    """A response object whose fields are exactly those declared."""

    model_config = ConfigDict(extra='forbid')


class _Target(_Response):
    """What a job names."""

    type: TargetType
    id: str = Field(description='The video or playlist ID.')


class _Media(_Response):
    """An item's media (DESIGN 5.7)."""

    outcome: MediaOutcome | None = Field(description="Null means not yet known, or not attempted on a failed item.")
    path: str | None = Field(description='Relative to the staging root; where the file was when the item ended.')


class _Track(_Response):
    """The caption track an item used (DESIGN 5.6)."""

    code: str = Field(description="YouTube's language code for the track.")
    kind: TrackKind


class _Captions(_Response):
    """An item's captions (DESIGN 5.7)."""

    outcome: CaptionsOutcome | None = Field(description="Null means not yet known, or not attempted on a failed item.")
    path: str | None = Field(description='Relative to the staging root; where the file was when the item ended.')
    track: _Track | None = Field(description='The track used, when captions were fetched rather than found existing.')


class _Item(_Response):
    """One video of a job."""

    id: str
    job_id: str
    position: int = Field(description='The position in the playlist; 0 for a single video.')
    video_id: str
    title: str | None
    state: ItemState
    media: _Media | None = Field(default=None, description="Present only when the job requests media.")
    captions: _Captions | None = Field(default=None, description='Present only when the job requests captions.')
    created_at: str
    updated_at: str


class FailureResponse(_Response):
    """A record that something did not succeed (DESIGN 5.9)."""

    id: str
    created_at: str
    job_id: str
    job_kind: JobKind
    item_id: str | None = Field(description='Null when the failure is the expansion of a playlist.')
    url: str
    video_id: str | None
    playlist_id: str | None
    playlist_title: str | None
    phase: Phase
    category: Category = Field(description='A hint at whether resubmitting helps.')
    message: str = Field(description='The raw error text.')


class JobResponse(_Response):
    """One submission, with its items and uncleared failures (DESIGN 5.8, 5.9)."""

    id: str
    kind: JobKind
    url: str
    target: _Target
    languages: list[str] | None = Field(description='Caption language candidates; null for a videos job.')
    state: JobState
    playlist_title: str | None
    created_at: str
    updated_at: str
    items: list[_Item]
    failures: list[FailureResponse]


class JobList(_Response):
    """The status table (DESIGN 5.8)."""

    jobs: list[JobResponse]


class FailureList(_Response):
    """Failures, oldest first (DESIGN 5.9)."""

    failures: list[FailureResponse]


class _DatabaseHealth(_Response):
    """Whether the database is readable and writable."""

    ok: bool
    error: str | None


class _StagingHealth(_Response):
    """The worker's most recent marker check (DESIGN 5.14)."""

    ok: bool
    marker_present: bool | None = Field(description='Null before the first check.')
    checked_seconds_ago: float | None = Field(description='Null before the first check.')
    paused: bool = Field(description='Whether work is paused because the marker is missing.')


class _Threads(_Response):
    """Which background threads are alive."""

    worker: bool


class Health(_Response):
    """Service health; 200 and 503 carry the same body (DESIGN 5.14)."""

    healthy: bool
    database: _DatabaseHealth
    staging: _StagingHealth
    threads: _Threads
    yt_dlp_version: str
