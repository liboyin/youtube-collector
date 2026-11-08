# youtube-collector: Requirements and Design

This document owns the product requirements, system design, product contracts and accepted user decisions. [README](README.md) covers setup and operation; [TODO](tasks/TODO.md) owns outstanding work and unresolved choices; [AGENTS](AGENTS.md) and the repository skills own working procedures.

Status: accepted requirements, written 2026-11-06 as a product requirements document (originally `PRD.md`) in a design discussion held in the `youtube-whisperer` repository. They include assumptions that stand unless the user objects. **The service is implemented but not yet run against real YouTube** (task [T25](tasks/open/T25.md)): sections 5, 6 and 10 describe the required behavior, which the components in section 14 implement once decisions C39–C47 are built (until then those sections run ahead of the code, and section 14 records what exists); section 7 records which deployment and development-environment parts the scaffolding tasks have implemented, and section 14 records the application components built so far. As components are built, record their architecture in section 14, and record later decisions with new IDs after the last one in section 11.

Requirement words (MUST, SHOULD, MAY) follow BCP 14. Behavior inherited from `youtube-whisperer` cites that project at revision `474f5dc` (the "source revision"); it describes what the code did there, which the collector reimplements rather than imports.

## 1. Purpose and context

`youtube-whisperer` currently downloads YouTube media and captions, transcribes media that lacks captions, and stores everything in one asset directory. The user is splitting it up: `youtube-collector` takes over YouTube acquisition, and `youtube-whisperer` becomes an orchestrator that calls it. A separate `media-transcriber` project may take over transcription later; see `TRANSCRIPTION_REDESIGN.md` in `youtube-whisperer`.

The collector is a standalone, LAN-only HTTP service running in Docker. Callers submit download jobs, receive a job identifier, and track them by polling. It writes into a **staging area**: the existing `ASSETS_DIR`, where downloaded videos and captions wait for transcription and transcoding before the user moves them into a permanent video library. Files disappear from staging routinely, and the collector must treat that as normal.

## 2. Goals and non-goals

Goals:

- Download single videos and playlists as MP4, YouTube captions as SRT, or both, into the staging area.
- Give each submission a job identifier with per-video state, outcomes and failure reasons.
- Let callers follow a job by polling it, without push channels.
- Talk to YouTube through one connection at a time, so one account's traffic stays modest.
- Keep failures visible until the user reviews them, or until a later attempt succeeds.

Non-goals for this version:

- Channels and channel tabs. Their URLs MUST be rejected.
- A command-line interface. The HTTP API and its generated `/docs` page are the only interfaces.
- Cancellation or automatic retry of jobs and items. Retry means submitting again.
- Metadata sidecars (`.info.json`) and comment collection. Comments belong to this project's future scope, not this version.
- Transcription, or deciding whether transcription is needed. The collector reports facts, such as "captions unavailable"; the orchestrator decides.
- Overwriting existing files. Every write uses the `NEVER` policy.
- Authentication, multi-user use, or exposure beyond a trusted LAN.
- Horizontal scaling. Exactly one collector process runs.

## 3. Consumers

| Consumer | Uses the collector to |
|---|---|
| `youtube-whisperer` orchestrator | Submit single-video jobs, poll each job it submitted, learn which videos lack captions, and record failures on its own side |
| The user | Submit jobs directly, watch the share for the files, check errored tasks, fix causes (cookies, yt-dlp version), clear reviewed failures |

The collector serves two paths (decision C47).

Manual download:

1. The user submits a video or playlist job.
2. The collector writes the files into the staging area (5.4).
3. The user waits for all the files to appear and moves them to the video library. The collector writes no indicator into the staging area: `GET /failures` lists errored tasks, and a job's record also shows `unavailable` captions, which are not failures (5.7, 5.9).

Orchestrator:

1. The orchestrator submits a single-video URL, never a playlist.
2. The collector returns the job's identifier.
3. The orchestrator reads the job at regular intervals (5.10).
4. When the job's turn arrives, the collector writes the files into the staging area (5.3, 5.4).
5. The next read shows that the files are ready, and the orchestrator moves on (section 10).
6. The user waits for all the files and moves them to the video library.

## 4. Terms

- **Job:** one accepted submission: a URL, a job kind, and for caption jobs a language candidate list.
- **Job kind:** `videos` (MP4 only), `captions` (SRT only) or `videos-with-captions` (both).
- **Item:** one video within a job. A single-video job has one item; a playlist job has one item per playlist entry.
- **Artifact:** a file an item produces: the media (MP4) or the captions (SRT).
- **Staging area:** the directory configured as `ASSETS_DIR`, an SMB mount in the user's deployment, usually holding 10 to 20 files.
- **Status table:** the collector's record of active jobs and of finished jobs still within the retention period (5.8).
- **Failure:** a self-contained record that something did not succeed, kept until the user clears it or a later attempt supersedes it.

## 5. Functional requirements

### 5.1 Submission

There MUST be three submission endpoints, one per job kind:

| Endpoint | Writes | `languages` |
|---|---|---|
| `POST /jobs/videos` | `<videoID> - <Title>.mp4` | not accepted |
| `POST /jobs/captions` | `<videoID> - <Title>.srt` | required |
| `POST /jobs/videos-with-captions` | both | required |

Request body fields:

- `url` (required): a single-video URL or a playlist URL (5.2).
- `languages`: a non-empty, ordered list of caption language candidates (5.6). Each entry MUST be a well-formed BCP 47 tag; there is no whitelist of supported languages. The list expresses alternatives for one language and produces at most one SRT.

A valid submission returns 202 with the job's initial representation and its identifier, and does not contact YouTube. The API validates URLs syntactically only, because only the worker thread may talk to YouTube (5.3). Malformed bodies, unsupported URLs and channel URLs return 422. Every valid submission creates a new job; submitting the same URL again is harmless, because the later job finds the published files `existing` (5.4, decision C46).

### 5.2 Supported URLs

- Single videos: the forms accepted by `get_video_id` at the source revision: `youtu.be/<id>`, and `/watch?v=<id>`, `/live/<id>` and `/shorts/<id>` on `youtube.com`, `www.youtube.com` and `m.youtube.com`, over HTTP or HTTPS.
- Playlists: `youtube.com/playlist?list=<id>` on the same hosts.
- Channels, handles (`/@name`), channel tabs and anything else MUST be rejected.
- YouTube Mix playlists, whose IDs begin with `RD`, are generated and effectively unbounded, so playlist URLs with such IDs MUST be rejected.

A watch URL that carries both `v=` and `list=` MUST be treated as the single video, and its `list=` parameter ignored. This differs from the source revision, where yt-dlp's default expanded such a URL into the whole playlist. A caller wanting the playlist submits its `/playlist?list=` URL, which makes the intent explicit.

### 5.3 Scheduling and the YouTube connection

- All YouTube traffic MUST run on a single worker thread, one request at a time: playlist expansion, video metadata lookup, media download and caption fetching. API request handlers MUST NOT contact YouTube.
- Expanding a playlist is the first step of a playlist job. Its entries become items in playlist order and MUST be persisted, so a restart does not expand the playlist again. Entries that yt-dlp returns as empty or without an ID, typically deleted or private videos, are skipped, as at the source revision.
- The worker MUST schedule items round-robin across active jobs: after finishing an item, it takes the next queued item from the next job in submission order. A long playlist therefore cannot block a later single-video job.
- Request pacing (for example yt-dlp's sleep intervals) MUST come from configuration, not from requests.

### 5.4 Output layout and naming

- A single-video job writes to the staging root. A playlist job writes into `<playlistID> - <Playlist title>/` under the staging root, created when missing.
- The worker MUST create a missing target directory again before each item, because the user may move a playlist folder away while its job runs.
- Media and captions are named `<videoID> - <Title>.mp4` and `<videoID> - <Title>.srt` (decision C45). The title and ID have OS-reserved characters replaced; only the title is truncated, at a UTF-8 character boundary, so the whole name fits in 220 bytes and the ID prefix and suffix always survive. A title with nothing left to keep fails the item. Playlist folder names follow the same rule with the playlist ID and title, and lose any trailing dots and spaces, which SMB shares do not accept at the end of a name. YouTube IDs can begin with `-`, so names can too.
- The source revision's reuse of legacy title-only files is dropped, and files named under an earlier scheme, such as `Title [id].mp4`, are not recognized (decision C44). The collector only ever looks for and writes ID-prefixed names.
- A video that appears in several playlists, or both in a playlist and at the root, is downloaded separately into each location. Duplication is accepted.
- Every write uses the `NEVER` policy, checked per artifact before any network work: an existing MP4 is not downloaded again, and an existing SRT is not fetched again. Each artifact is judged independently, so a job can skip an existing MP4 and still fetch its missing SRT.
- An existing artifact is recognized by its video ID within the destination directory, as a file named `<videoID> - <anything>.mp4` or `<videoID> - <anything>.srt`, whatever its title part says (decisions C23, C45). A video renamed on YouTube therefore still counts as existing, and the check needs only the ID, which the URL or the persisted playlist entry supplies. Matching treats the ID literally, not as a glob pattern. Any match counts as existing, even when several files carry the same ID and suffix. A new SRT takes the stem of the existing MP4 when exactly one exists, so the pair stays together after a title change; otherwise it uses the current title (decision C33).
- Publication MUST be atomic, as at the source revision: each artifact is written inside a fresh hidden `.staging-<random>` directory beside its destination, and only a complete file is published, using pathlib-extensions' `StagedFile`. A failure or interruption leaves previous output intact and removes its own stage directory. A stage directory left by an abrupt process exit is not scavenged automatically; tools that move files out of staging SHOULD skip hidden directories.

### 5.5 Media

Media MUST be MP4, using the source revision's yt-dlp format selection, `bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]`, and its options: Firefox cookies when a profile is detected (5.11) and the Deno JavaScript runtime.

### 5.6 Caption language matching

YouTube lists manually created and auto-generated caption tracks, each with a language code. The collector chooses one track as follows:

1. A candidate matches a track when the candidate's subtags, compared case-insensitively, are a prefix of the track code's subtags at a subtag boundary (RFC 4647 basic filtering). `zh` matches `zh`, `zh-TW`, `zh-CN`, `zh-Hans` and `zh-Hant`. `zh-cn` matches `zh-CN` but not `zh-TW`, `zh-Hans` or plain `zh`. `zh` does not match `zha`.
2. There is no fallback to a shorter tag: `zh-cn` never falls back to a plain `zh` track. A caller wanting that lists `zh` as a later candidate.
3. Manual tracks win across the whole candidate list: the candidates are tried in order against manual tracks, then in order against auto-generated tracks.
4. Within one candidate and one track kind, an exact match wins, then YouTube's listed order.
5. Auto-translated tracks are never selected.

The item MUST record which track it used, as its code and kind (manual or auto-generated).

This deliberately differs from the source revision. There, `get_first_matching_lang_code` tried the request's BCP form against all tracks before its ISO form, so an auto-generated `zh-CN` could beat a manual `zh-Hans`. It also used plain string prefixes, so a request could match an unrelated longer code, and `zh-cn` fell back to `zh`. Its fixed whitelist (`en`, `en-us`, `zh`, `zh-cn`) and BCP/ISO conversion remain in `youtube-whisperer`, which needs them for Whisper.

Captions are converted to SRT with `youtube-transcript-api`'s `SRTFormatter`, as at the source revision. Caption HTTP requests MUST keep the source revision's 10-second connect and 30-second read timeouts (decision D38). As there, the read timeout bounds silence between bytes, not total transfer time.

### 5.7 Item processing and outcomes

Each item records an outcome for every artifact its job kind requests:

| Artifact | Possible outcomes |
|---|---|
| Media | `downloaded`, `existing`, `failed` |
| Captions | `downloaded`, `existing`, `unavailable`, `error`, `failed` |

A `videos-with-captions` item downloads media first. If media fails, captions are not attempted and the item fails. If media succeeds, a caption-service error (any `youtube-transcript-api` service exception or `requests` exception, including timeouts) gives captions the outcome `error`. The item still counts as done, because its media succeeded, but the error also produces a failure record (5.9). Disabled captions or no matching track give `unavailable`, which is a definitive answer, not a failure. Any other caption error, such as a malformed caption payload or an SRT publication error, fails the item, as at the source revision.

In a `captions` item, the captions are the whole purpose, so a caption-service error fails the item; `unavailable` remains a successful outcome.

A requested artifact's outcome stays null when the item fails before that outcome is determined. An outcome already determined is kept: an artifact found `existing` before the failure stays `existing`. So when the destination directory cannot be listed, both outcomes stay null; when the metadata request fails, every outcome not already `existing` stays null; when media fails, the captions outcome stays null unless the captions already existed. Such an item is `failed`, and its failure record names the phase (5.9). Clients therefore treat a null outcome on a `failed` item as "not attempted"; on a `queued` or `running` item it means "not yet known".

Item states are `queued`, `running`, `done` and `failed`. A failure in one item never stops the remaining items of its job (decision D17 at the source revision). Job states are `queued`, `expanding`, `running` and `finished`; a job whose playlist expansion fails becomes `finished` with no items and a failure record.

### 5.8 Status table and retention

The status table holds jobs with their items. A job stays readable through `GET /jobs` and `GET /jobs/{id}` while it is `queued`, `expanding` or `running`, and for a configured retention period after it finishes (assumed 7 days), whatever has happened to its files (decision C40). A job that has not finished MUST NOT be deleted, however old it is.

Retention is enforced when a submission arrives, in the submission's own transaction: every job past the retention is deleted with its items. No background task is needed, because only submissions add records. The retention period is therefore a lower bound: without new submissions, an expired job stays readable until the next one arrives (decision C42).

The collector does not follow its files after publishing them. A path in an item says where the file was published or found when the item ended; files disappearing from staging change no record, and a caller that needs a file looks at the share (decision C40). Uncleared failures are not subject to retention: they stay until cleared or superseded (5.9), also after their job is deleted.

API request handling MUST NOT perform staging filesystem calls itself and MUST stay responsive while the share is stalled, because a stalled SMB share can block a metadata call indefinitely and a thread timeout does not cancel blocked I/O (decision C25). Only the worker thread touches the share.

### 5.9 Failures

Failures are their own resource, separate from items, and carry everything needed to understand them after their item or job is gone. A job's representation also embeds its uncleared failures, so one read tells a caller how the job ended (decision C39). A failure has:

- `id`, `created_at`, `job_id`, `job_kind`, and `item_id` when the failure belongs to an item
- `url`, `video_id`, and playlist ID and title when known
- `phase`: `expanding`, `metadata`, `media` or `captions`
- `category`, a hint at whether resubmitting helps:
  - `unavailable`: private, removed, members-only or geo-blocked. Permanent.
  - `blocked`: bot check, sign-in required, or cookies rejected. Fix cookies, then resubmit.
  - `network`: timeouts and connection errors. Resubmit as is.
  - `extractor`: yt-dlp failed to parse YouTube's response. Usually fixed by updating yt-dlp.
  - `filesystem`: a staging write or publication failed.
  - `internal`: anything else.
- `message`: the raw error text, always kept

yt-dlp mostly raises one generic error type, so categories come from matching error messages. They are a documented hint that falls back to `internal`, and `message` is the authoritative detail.

A failure stays until the user clears it, or until it is **superseded**: a later item for the same video and the same artifact reaches a definitive result. For media that means `downloaded` or `existing`. For captions it means `downloaded`, `existing` or `unavailable`. A playlist expansion failure is superseded by a later successful expansion of the same playlist. `GET /failures` is how the user checks errored tasks (section 3). A cleared failure also disappears from its job's representation. The orchestrator reads failures but does not clear them.

### 5.10 Following a job

A caller follows a job by reading `GET /jobs/{id}` at regular intervals of its own choosing (decision C41). Each read returns the job as it stands, with its items and uncleared failures, from SQLite, the source of truth.

- An unknown or deleted job returns 404.
- There is no long polling and no stream of every change: a caller polls the jobs it submitted (decisions C39, C41).
- There is no byte-level progress: an item's state tells a caller whether it is waiting, running or finished, and its outcomes and paths say which files are ready (5.7).

### 5.11 Cookies

Cookie handling stays as at the source revision: yt-dlp uses Firefox cookies when a `cookies.sqlite` file is found under the standard Firefox profile locations, and the Compose file mounts the profile from `FIREFOX_PROFILE_DIR`. Detection happens once per process. The profile holds live session cookies; agents and tests MUST NOT read or copy it.

### 5.12 Restart recovery

On startup, items left `running` return to `queued`, and a job interrupted during expansion expands again. Because every artifact is checked with `NEVER` before work, repeating an item that had already published its MP4 skips the download. A partial download is never resumed; it restarts in a fresh stage directory.

On SIGTERM the worker stops taking items and lets the current yt-dlp call finish, abandoning it when the shutdown grace period ends; the interrupted item is then recovered by the rule above on the next start (decision C36). The grace period is an implementation default.

### 5.13 Staging availability

An unmounted SMB share can look like an empty directory, and downloads would then be written into the container instead of the share. The staging root MUST therefore contain a marker file, `.youtube-collector-root`, which the user creates once.

- Before starting each item, and again before publishing each artifact, the collector MUST check that the marker exists.
- While the marker is missing or the staging root cannot be read, the worker MUST pause without changing any record: no items start or fail. It resumes on its own once the marker is visible again. An item already running when the share disappears fails with category `filesystem`, as any write failure does.
- A stalled share, where filesystem calls block instead of failing, is accepted to stall the YouTube worker for as long as the stall lasts; it MUST NOT stall the API (5.8, decision C25).
- `GET /health` reports the paused state (5.14), and the collector logs a warning when it pauses and when it resumes.
- The collector never creates the marker itself, because creating it on an unmounted mount point would defeat the check.

### 5.14 Health

`GET /health` MUST report, without contacting YouTube:

- whether the database is readable and writable
- whether the staging marker is present, and therefore whether work is paused
- whether the worker thread is alive
- the installed yt-dlp version

It returns 200 when everything is healthy and 503 otherwise, with the same body in both cases. The staging result is the worker thread's most recent marker check, reported with its age, so `/health` itself never touches the share and stays responsive during an SMB stall (decision C25). The worker repeats the check periodically during a download, so a long download does not look like a stall (decision C43). The age beyond which a cached result counts as unhealthy is an implementation default, not part of the contract (decision C35).

## 6. HTTP API summary

| Endpoint | Purpose |
|---|---|
| `POST /jobs/videos` | Submit a media job |
| `POST /jobs/captions` | Submit a captions job |
| `POST /jobs/videos-with-captions` | Submit a media-and-captions job |
| `GET /jobs` | The status table: every retained job with its items and uncleared failures (5.8) |
| `GET /jobs/{id}` | One job with its items and uncleared failures (5.10) |
| `GET /failures` | All uncleared failures, oldest first |
| `DELETE /failures` | Clear all failures; returns the cleared records |
| `DELETE /failures/{id}` | Clear one failure |
| `GET /health` | Service health and paused state (5.14) |

An unknown job or failure returns 404. Validation errors return 422. Unexpected errors return a generic 500 and are logged, as at the source revision. Exact JSON schemas are left to implementation. FastAPI's OpenAPI document, browsable at `/docs`, publishes the request bodies' schemas and describes every endpoint; responses are untyped JSON objects whose fields this document and the endpoint descriptions define. Typed response models are task [T31](tasks/open/T31.md).

## 7. Deployment and operation

- One Docker Compose service runs one process: the HTTP server and the single YouTube worker thread. It MUST NOT run several server processes.
- The SQLite database MUST live on a Docker named volume, never in `ASSETS_DIR`. Container storage is local, unlike the SMB-mounted staging area where SQLite locking is unreliable, and a named volume survives container recreation, including the image rebuilds that update yt-dlp. SQLite SHOULD run in WAL mode.
- `ASSETS_DIR` is bind-mounted as the staging area, and `FIREFOX_PROFILE_DIR` as the cookie profile, as in `youtube-whisperer`'s Compose file.
- The service is unauthenticated and LAN-only, as decision D5 states for `youtube-whisperer`. It MUST NOT be exposed to the public internet.
- Dependencies are locked with uv and installed through the shared `docker-build-common` helpers, as in `youtube-whisperer` (D50, D52). yt-dlp breaks whenever YouTube changes and ships fixes often, so the project MUST provide a script that runs `uv lock --upgrade-package yt-dlp` and applies the new lock to the devcontainer's venv, following `youtube-whisperer`'s `update-dependencies.sh`. Rebuilding the image is a separate step run on the host, because the devcontainer has no Docker (decision C29, superseding C19's "rebuilds the image"). Other dependencies keep their locked versions.
- `pyproject.toml` declares no upper version bounds. `uv.lock` makes builds reproducible, and every full upgrade's `uv.lock` diff is reviewed before commit (decision C32).
- The image is built from `ubuntu:26.04`, whose distro `python3` is Python 3.14, keeping the distro-Python and `ubuntu`-user design (decision C22, superseding T1's `ubuntu:24.04` and Python 3.12).
- The devcontainer MUST NOT inherit the production mounts (decision C28). Its default configuration replaces them with development-only storage: a throwaway staging volume, its own database volume and no browser profile, whatever `.env` sets. A second, explicitly named configuration (`.devcontainer/real-data/`, labelled "REAL DATA") adds the real staging share and a read-only Firefox profile back for sessions the user deliberately opens; it fails to start unless `ASSETS_DIR` and `FIREFOX_PROFILE_DIR` are set, sets `COLLECTOR_REAL_DATA=1`, and keeps the development database, because a development collector and the production service sharing one SQLite file could corrupt each other's records. Opening it authorizes nothing: each run against real YouTube still needs the user's authorization for that run, and tests still use only test-owned storage. The Compose service has no fixed `container_name`, and the devcontainer and the production service have distinct Compose project names (`youtube-collector` and `youtube-collector-service`, T13), so they can coexist on one Docker daemon.
- The production Compose service mounts the Firefox profile read-only; yt-dlp copies `cookies.sqlite` to a temporary directory before opening it (verified in the locked yt-dlp source on 2026-11-07; the mount itself is unverified).
- The devcontainer uses VS Code's SSH agent forwarding instead of bind-mounting `~/.ssh`, so private key files are not present in the container; processes in it can still use the forwarded agent while it is connected (decision C31).
- The user creates the staging marker (5.13) once, at the root of the share.
- Each thread owns its own SQLite connection, and every write is one short transaction (decision C36).

Implemented by T1, T2, T3, T4, T5, T8 and T13 ([records](tasks/closed/)): a Dockerfile with four stages from a digest-pinned `ubuntu:26.04`, using the shared helpers. `base` holds ca-certificates, ffmpeg, distro Python 3.14 and Deno copied from a digest-pinned `denoland/deno` binary image; `build` adds git and uv, checks `uv.lock` against `pyproject.toml` offline, and builds the locked venv at `/opt/venv` with the project as a regular package; `runtime` is `base` plus that venv, without git, uv, curl or the source tree; `dev` continues from `build` with curl, the dev group and an editable project. The single Compose service `app`, in Compose project `youtube-collector-service`, has the staging, read-only Firefox-profile and `collector_db` mounts (the database directory is `/var/lib/youtube-collector`) and no fixed container name. The devcontainer, in Compose project `youtube-collector`, replaces those mounts with development-only volumes, offers the opt-in real-data configuration, pins its feature to a major version with a committed lock, and its creation hook checks the lock offline and records GitHub's SSH host keys. `./update-dependencies.sh yt-dlp` fulfils the yt-dlp update as C29 restates it; rebuilding the image on the host deploys it. [TODO](tasks/TODO.md) tracks the remaining review tasks. T24 added the service command (`python3 -m youtube_collector`), its environment, the published port (`COLLECTOR_PORT`, default 8000), `restart: unless-stopped` and a 45-second stop grace period; the dev container resets the port and restart policy. [README](README.md) owns setup and configuration.

Configuration, read from the environment by `config.Settings` (T24); [README](README.md#configuration) owns the full table:

| Setting | Variable | Default |
|---|---|---|
| Staging directory inside the container | `COLLECTOR_STAGING_DIR` | required |
| Database path | `COLLECTOR_DATABASE` | `collector.sqlite3` on the named volume |
| YouTube request pacing | `COLLECTOR_SLEEP_REQUESTS`, `COLLECTOR_SLEEP_DOWNLOAD`, `COLLECTOR_MAX_SLEEP_DOWNLOAD` | yt-dlp's defaults (no extra sleep) |
| Retention of finished jobs (5.8) | `COLLECTOR_JOB_RETENTION_DAYS` | 7 days |
| Shutdown grace for the current download (C36) | `COLLECTOR_SHUTDOWN_GRACE` | 30 seconds |

Implementation defaults that are not settings (C35, C43): the worker repeats its marker check at least every 30 seconds during a download; a cached staging check older than 120 seconds counts as unhealthy; uvicorn gives open requests 5 seconds after SIGTERM.

## 8. Behavior carried over and dropped

Carried over from `youtube-whisperer` at the source revision, reimplemented here:

| Behavior | Source |
|---|---|
| Truncation of names to 220 bytes, keeping the ID whole | D47; `downloaders/utils.py` |
| Atomic staged publication; `NEVER` preserves competing files | D22, D48, D49; pathlib-extensions `StagedFile` |
| Per-video failure isolation within a playlist | D17 |
| Caption-service errors after a media download do not fail the item | D16 |
| Caption request timeouts of 10 s connect and 30 s read | D38 |
| Skipping deleted or private playlist entries | `yield_video_urls_from_info` |
| MP4 format selection, Deno runtime and Firefox cookie detection | `video_downloader.py`, `downloaders/utils.py` |
| Single-video URL forms | `get_video_id` |
| LAN-only, unauthenticated trust model | D5 |

Changed or dropped:

| Behavior at the source revision | In the collector |
|---|---|
| Redis Streams, consumer groups and the dead-letter list | SQLite status table and the failure resource |
| Caption-service errors logged only, not dead-lettered (D39) | Recorded as failures, because resubmitting costs only the SRT fetch |
| One language, BCP form tried before ISO form, plain string prefix | Candidate list, manual first, subtag-boundary matching (5.6) |
| `Title [id].ext` naming (D47) | `<id> - <Title>.<ext>` (C45) |
| Reuse of legacy title-only files (part of D47) | Dropped |
| Enqueuing transcription follow-ups and the `none` transcriber | Dropped; the orchestrator decides |
| Channels and channel tabs | Rejected |
| Filesystem glob tasks, `GET /assets`, `DELETE /assets` | Not part of the collector |
| Completion inferred only from files on disk | Status table with time-based retention |

## 9. Engineering standards

The repository adopts `youtube-whisperer`'s working standards: its AGENTS.md, the adversarial review and grouped task execution skills, the coverage gate, mypy and ruff, adapted on 2026-11-06 from `youtube-whisperer` revision `474f5dc`. The rationale for those standards is recorded in that revision's `DESIGN.md` under "Engineering governance" (decisions D1, D4 and D28–D44) and is not copied here. Adaptations: references to Redis, Azure and Whisper were replaced with the collector's own resources, the test-import rule names `youtube_collector`, and running anything against real YouTube needs the user's authorization for each run. [AGENTS](AGENTS.md#tests) and [tests/README.md](tests/README.md) own the testing rules, including what tests may read and write and the bounded wall-time checks they allow. As a design requirement, the worker thread MUST be drivable in tests by injected clocks and explicit completion signals.

## 10. Integration with youtube-whisperer

Switching the orchestrator over is a later, separate phase. The collector SHOULD first run alongside the existing system, proving itself before anything depends on it. The intended use:

1. The orchestrator submits a `videos-with-captions` job for a single video URL, never a playlist, and saves the returned job ID.
2. It reads `GET /jobs/{id}` at regular intervals until the job is `finished`, and after a restart resumes from the job IDs it saved. If it crashed before saving an ID, it submits again: the later job finds the published files `existing`, and the earlier job expires unread (decision C46).
3. If the item's captions are `unavailable` or `error`, it maps the item's staging path to its own mount of the same directory and requests transcription. The decision moves out of `YouTubeWorker.process_video`. If the file has left staging by then, the transcription request fails, and the orchestrator treats that as the user having moved the file (decision C40).
4. It records the failures the job carries and does not clear them. It treats a missing job (404) as "nothing left to do": the job has passed its retention.

Paths in responses are relative to the staging root, so the collector and orchestrator may mount the staging area at different container paths.

## 11. Decisions agreed in discussion (2026-11-06)

| ID | Decision |
|---|---|
| C1 | The collector is its own Dockerized service with its own work queue, superseding `TRANSCRIPTION_REDESIGN.md`'s recommendation of one orchestrator-owned queue. The queue is SQLite. |
| C2 | One YouTube connection at a time, from a single thread. |
| C3 | Write into the existing `ASSETS_DIR`, a staging area from which the user removes files. |
| C4 | Playlists get their own folder, whose name C45 now sets; duplicates across playlists are accepted. |
| C5 | Polling only. No webhooks or Server-Sent Events. Long polling was part of this decision and is superseded by C41. |
| C6 | No CLI, superseding the original idea of offering one. |
| C7 | LAN-only and unauthenticated. |
| C8 | The status table lists pending work and completed work whose files remain in staging, refreshed by reconciliation; there are endpoints to view the status table and to clear failures. The mirroring of staging and the reconciliation are superseded by C40; the endpoints stand. |
| C9 | Round-robin scheduling across jobs. |
| C10 | The database lives on a Docker named volume, not on the SMB share. |
| C11 | Three submission endpoints: videos, captions, and videos with captions. MP4 only; channels out of scope; no cancellation or retry; `NEVER` overwrite only. |
| C12 | Caption language candidates as an ordered list, matched manual-first, with subtag-boundary matching and no fallback to shorter tags. Any well-formed language tag is accepted. |
| C13 | Failures carry their reason, stay until the user clears them, and are cleared automatically when a later attempt succeeds. Caption errors in combined jobs appear as failures. |
| C14 | Cookies keep the current Firefox profile detection. |
| C15 | The repository adopts `youtube-whisperer`'s governance, adapted in a later step. |
| C16 | A watch URL with both `v=` and `list=` is the single video; playlists are submitted by `/playlist?list=` URL. |
| C17 | A marker file at the staging root guards against an unmounted share; work pauses while it is missing. |
| C18 | `GET /health` reports database, staging marker, thread liveness and yt-dlp version. |
| C19 | A script updates yt-dlp alone and rebuilds the image. Superseded by C29. |
| C20 | Progress reports bytes, and a percentage when the total is known. Superseded by C41. |
| C21 | YouTube Mix playlists are rejected. |

Decisions from the project review (2026-11-07), made by the user after two independent reviews of revision `ca869a8`:

| ID | Decision |
|---|---|
| C22 | Build from `ubuntu:26.04` with its distro Python 3.14, rather than `python:3.14-slim`, to keep the distro-Python and `ubuntu`-user design. Supersedes T1's `ubuntu:24.04` and Python 3.12 direction. |
| C23 | Recognize an existing artifact by video ID within its destination directory, including after the video's title changes. |
| C24 | Retain idempotency keys for at least seven days after their job is removed. Superseded by C46. |
| C25 | Keep the API responsive during SMB stalls: no staging filesystem calls on the API path, cached staging health reported with its age, and a stalled worker or reconciliation thread accepted. Stands, with the worker as the only thread that touches the share (C40). |
| C26 | Reconciliation removes only `finished` jobs. Superseded by C40; retention likewise deletes only finished jobs. |
| C27 | 410 applies when `since` is below a durably recorded pruned-through revision. Superseded by C39. |
| C28 | The devcontainer uses development-only storage by default; a separate, explicitly named real-data configuration mounts the real share and a read-only Firefox profile, never the production database (section 7). The fixed Compose `container_name` is removed. |
| C29 | Supersedes C19: the update script updates `uv.lock` and the devcontainer's venv; rebuilding the image is a separate host step. |
| C30 | The orchestrator records the outcome and failures carried by a tombstone but never requests transcription from it. Superseded by C39 and C40. |
| C31 | The devcontainer uses SSH agent forwarding instead of a `~/.ssh` bind mount. |
| C32 | No upper bounds on dependencies; rely on `uv.lock` and review the lock diff on every full upgrade. |

C26 and C27 correct contradictions that both reviews found, rather than choosing between alternatives.

Decisions on the review's open questions (2026-11-07), made by the user. Q3 was settled later the same day as C38.

| ID | Decision |
|---|---|
| C33 | Settles Q1 (refines C23): any file matching the video ID and suffix counts as existing, even when several do. A new SRT takes the stem of the existing MP4 when exactly one exists; otherwise it uses the current title (5.4). |
| C34 | Settles Q2 (refines C24): the retained idempotency key stores the job's final representation, which a replay after removal returns with 200; a different body under a retained key returns 409; an expired key is forgotten and its reuse creates a new job (5.1). Superseded by C46. |
| C35 | Settles Q4: C25 stands unchanged. How long `GET /jobs` waits for a requested reconciliation, and the age at which cached staging health counts as unhealthy, are implementation defaults rather than contract details, so they block no task (5.8, 5.14). The reconciliation wait no longer exists (C40). |
| C36 | Settles Q5: on SIGTERM the worker stops taking items, lets the current yt-dlp call finish or abandons it at the grace period, and relies on restart recovery; each thread owns its SQLite connection, and every write is one short transaction (5.12, section 7). The transaction also assigned the revision until C39 removed it. |
| C37 | Settles Q6: playlists are not capped in size, and the API does not validate the `Host` header against DNS rebinding. Neither is a demonstrated defect, and the service stays LAN-only (C7). |
| C38 | Settles Q3 (refines C27), after the analysis in section 12: change pages hold each record once in its latest state, ordered by revision, with the last revision on the page as the next cursor; `GET /jobs` and `GET /failures` each return the revision of their snapshot, and a resynchronizing caller follows the feed from the lower one; cursors stay plain revisions, and a cursor kept across a recreated database is not detected, a risk the user accepted because recreating the volume is deliberate and the orchestrator can be reset by hand (5.10). Superseded by C39. |

Decisions on the 2026-11-08 architecture review (`architecture-2026-11-08.md` at revision `2362807`) and on the two workflows the user then described, made by the user. The review's question was which features cost more complexity than one user, one consumer and a few jobs a day justify. The user answered that nothing needs a single stream of every change, that looking at the share is enough to know what is still in staging, and that no consumer constrains the API yet. C42–C44 settle what those answers left open, and C41 and C45–C47 follow from the workflows in section 3. [T29](tasks/closed/T29.md) records each proposal's disposition.

| ID | Decision |
|---|---|
| C39 | Settles P1 (supersedes C27, C30 and C38): there is no global change feed, revision counter, tombstone, cursor, 410 or two-snapshot resynchronization. A caller reads the jobs it submitted. The job representation embeds its uncleared failures, so "submit, poll, read" is the whole protocol; a cleared failure disappears from it. |
| C40 | Settles P3 with Option A (supersedes C8's mirroring and reconciliation, and C26): the status table does not mirror the staging filesystem and there is no reconciliation thread. Jobs are retained by time, and the worker's marker check before writing (C17) remains. An item's path records where the file was; the orchestrator learns that a file moved when its transcription request fails. |
| C41 | Goes beyond P2 (supersedes C5's long polling and C20): there is no long polling, per-job version or byte-level progress, because neither workflow needs them. Callers read `GET /jobs/{id}` at their own interval. C5's exclusion of push channels stands. |
| C42 | Retention needs no background actor: the submission transaction deletes expired jobs. Only submissions add records, so this bounds the table. Retention is therefore a lower bound on how long a job stays readable. |
| C43 | The worker owns the cached staging check that `/health` reads, and repeats it during downloads, because a download can outlast the staleness limit while the share is healthy. Only an unexpected silence on the worker thread, such as a stalled share or a long ffmpeg merge, reports stale. |
| C44 | Settles the review's fourth question, widened by the user: nothing in this project keeps backward compatibility. The API, file names, settings and database change without a compatibility layer or migration. The schema version rises and an older database file is refused at startup with an error naming its version. |
| C45 | Files are named `<videoID> - <Title>.<ext>` and playlist folders `<playlistID> - <Playlist title>/` (supersedes the `Title [id].ext` format of D47 and C4). An existing artifact is recognized by the `<videoID> - ` prefix; C23 and C33 otherwise stand. |
| C46 | There are no idempotency keys (supersedes C24 and C34). A repeated submission creates another job, which finds the published files `existing` without contacting YouTube. The accepted cost is one repeat download when the user has already moved the files; after a hard crash the user cleans up the staging area by hand. |
| C47 | The two workflows in section 3 define the product. The orchestrator submits only single videos; playlists are for manual use, and the API does not enforce the difference. The user learns of finished files from the share and of errored tasks from `GET /failures`; the collector writes no failure indicator into the staging area. |

P4 (running the reconciler as a coroutine or a subprocess) was rejected by the review and is moot under C40.

## 12. Open questions

No open questions remain. The 2026-11-07 review raised Q1–Q6, settled as C33–C38 (section 11); the analysis behind C38 is in this file at revision `2362807`.

## 13. Assumptions

These follow from repository evidence or the discussion, and stand unless the user objects:

- The API and worker share one uvicorn process, because a submission wakes the worker and `/health` reads the worker's cached staging check in memory.
- The tuning values in section 7 are starting defaults, not measured requirements.
- An item's title and ID come from a metadata request on the worker thread, as at the source revision; the implementation MAY use a playlist's flat-extraction entries instead, when they supply the same values.
- The collector writes only new files and never deletes anything from staging.
- Every job writes under the one configured staging root; a request cannot choose another directory.

## 14. Architecture

The application is one package, `youtube_collector`, with one module per responsibility. API request handlers never touch the staging share (C25): only the worker and the reconciliation thread call the staging layer.

Implemented, as built before decisions C39–C47:

| Module | Responsibility | Task |
|---|---|---|
| `urls` | Turns a submitted URL into a video or playlist target with a canonical URL, syntactically (5.2) | [T15](tasks/closed/T15.md) |
| `languages` | Checks that language candidates are well-formed BCP 47 tags and selects one caption track (5.6) | [T16](tasks/closed/T16.md) |
| `staging` | File and folder names, the root marker, existing-artifact lookup by ID, `NEVER` publication through a stage directory, and presence checks (5.4, 5.13) | [T17](tasks/closed/T17.md) |
| `categories` | Maps an error to a failure category and keeps its raw message (5.9) | [T18](tasks/closed/T18.md) |
| `store` | The SQLite database: jobs, items, failures, idempotency keys, one global revision counter and tombstones; one connection per thread and one `BEGIN IMMEDIATE` transaction per write, so revisions become visible in commit order (5.1, 5.3, 5.7–5.10, 5.12, section 7); change pages, snapshot revisions and job views for the feed (C27, C38) | [T19](tasks/closed/T19.md), [T23](tasks/closed/T23.md) |
| `youtube` | yt-dlp adapter: playlist expansion, one metadata extraction per video reused for its MP4 download, progress, pacing options and Firefox cookie detection (5.3, 5.5, 5.11) | [T20](tasks/closed/T20.md) |
| `captions` | Caption adapter: lists tracks, selects one with `languages`, fetches SRT, with 10 s connect and 30 s read timeouts (5.6, D38) | [T20](tasks/closed/T20.md) |
| `worker` | The YouTube worker thread: round-robin units, the marker pause, `NEVER` checks before network work, staged downloads, caption outcomes by job kind, throttled progress and restart recovery (5.3, 5.4, 5.7, 5.10, 5.12, 5.13) | [T21](tasks/closed/T21.md) |
| `reconcile` | The reconciliation thread: periodic and requested passes that remove items whose recorded files are gone and finished jobs left with nothing, with the marker checked before and after probing; cached marker results and pass times that the API reads without touching the share (5.8, 5.13, 5.14, C25, C26) | [T22](tasks/closed/T22.md) |
| `notify` | Thread-safe wake-ups for long-poll waiters on the event loop: tickets taken before each database read, so no change committed during a read is missed (5.10) | [T23](tasks/closed/T23.md) |
| `config` | Settings from the environment with section 7's defaults; the only module that reads the environment | [T24](tasks/closed/T24.md) |
| `api` | The FastAPI app and the service's composition: every endpoint in section 6, the lifespan that starts and stops both threads, database calls on one dedicated thread, long polls on the event loop, and health from cached results (5.1, 5.8, 5.10, 5.14, C25, C36) | [T24](tasks/closed/T24.md) |
| `__main__` | `python3 -m youtube_collector`: one uvicorn process | [T24](tasks/closed/T24.md) |

The first run against real YouTube is task [T25](tasks/open/T25.md).
