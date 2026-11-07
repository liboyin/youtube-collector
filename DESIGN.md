# youtube-collector: Requirements and Design

This document owns the product requirements, system design, product contracts and accepted user decisions. [README](README.md) covers setup and operation; [TODO](TODO.md) owns outstanding work and unresolved choices; [AGENTS](AGENTS.md) and the repository skills own working procedures.

Status: accepted requirements, written 2026-11-06 as a product requirements document (originally `PRD.md`) in a design discussion held in the `youtube-whisperer` repository. They include assumptions that stand unless the user objects. **No application behavior is implemented yet**: sections 5, 6 and 10 describe intended behavior, not current behavior, while section 7 records which deployment and development-environment parts the scaffolding tasks have implemented. As components are built, record their architecture here, and record later decisions after C21 with new IDs.

Requirement words (MUST, SHOULD, MAY) follow BCP 14. Behavior inherited from `youtube-whisperer` cites that project at revision `474f5dc` (the "source revision"); it describes what the code did there, which the collector reimplements rather than imports.

## 1. Purpose and context

`youtube-whisperer` currently downloads YouTube media and captions, transcribes media that lacks captions, and stores everything in one asset directory. The user is splitting it up: `youtube-collector` takes over YouTube acquisition, and `youtube-whisperer` becomes an orchestrator that calls it. A separate `media-transcriber` project may take over transcription later; see `TRANSCRIPTION_REDESIGN.md` in `youtube-whisperer`.

The collector is a standalone, LAN-only HTTP service running in Docker. Callers submit download jobs, receive a job identifier, and track progress by polling or long polling. It writes into a **staging area**: the existing `ASSETS_DIR`, where downloaded videos and captions wait for transcription and transcoding before the user moves them into a permanent video library. Files disappear from staging routinely, and the collector must treat that as normal.

## 2. Goals and non-goals

Goals:

- Download single videos and playlists as MP4, YouTube captions as SRT, or both, into the staging area.
- Give each submission a job identifier with per-video progress, outcomes and failure reasons.
- Let callers wait for changes efficiently through long polling, without push channels.
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
| `youtube-whisperer` orchestrator | Submit jobs, follow the change feed, learn which videos lack captions, and record failures on its own side |
| The user | Submit jobs directly, inspect the status table and failures, fix causes (cookies, yt-dlp version), clear reviewed failures |

## 4. Terms

- **Job:** one accepted submission: a URL, a job kind, and for caption jobs a language candidate list.
- **Job kind:** `videos` (MP4 only), `captions` (SRT only) or `videos-with-captions` (both).
- **Item:** one video within a job. A single-video job has one item; a playlist job has one item per playlist entry.
- **Artifact:** a file an item produces: the media (MP4) or the captions (SRT).
- **Staging area:** the directory configured as `ASSETS_DIR`, an SMB mount in the user's deployment, usually holding 10 to 20 files.
- **Status table:** the collector's record of pending work and of completed work whose files are still in staging.
- **Failure:** a self-contained record that something did not succeed, kept until the user clears it or a later attempt supersedes it.
- **Revision:** a monotonically increasing number stamped on every change to a job, item or failure, used by the change feed.

## 5. Functional requirements

### 5.1 Submission

There MUST be three submission endpoints, one per job kind:

| Endpoint | Writes | `languages` |
|---|---|---|
| `POST /jobs/videos` | `Title [id].mp4` | not accepted |
| `POST /jobs/captions` | `Title [id].srt` | required |
| `POST /jobs/videos-with-captions` | both | required |

Request body fields:

- `url` (required): a single-video URL or a playlist URL (5.2).
- `languages`: a non-empty, ordered list of caption language candidates (5.6). Each entry MUST be a well-formed BCP 47 tag; there is no whitelist of supported languages. The list expresses alternatives for one language and produces at most one SRT.
- `idempotency_key` (optional): a caller-chosen string. A submission whose key matches a retained key returns that key's job instead of creating a new one. Reusing a key with a different body MUST return 409. A key MUST be retained while its job is in the status table and for at least seven days after the job is removed, whatever the tombstone retention setting (5.10) is (decision C24). The retained key stores the job's final representation, because the job's tombstone may be pruned first; replaying the key after the job is removed returns that representation with 200, and a same-key request with a different body still returns 409 while the key is retained. An expired key is forgotten, so reusing it creates a new job (decision C34).

A valid submission returns 202 with the job's initial representation and its identifier, and does not contact YouTube. The API validates URLs syntactically only, because only the worker thread may talk to YouTube (5.3). Malformed bodies, unsupported URLs and channel URLs return 422.

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

- A single-video job writes to the staging root. A playlist job writes into `Playlist Title [playlistID]/` under the staging root, created when missing.
- The worker MUST create a missing target directory again before each item, because the user may move a playlist folder away while its job runs.
- Media and captions are named `Title [videoID].mp4` and `Title [videoID].srt`, following decision D47 at the source revision. The title and ID have OS-reserved characters replaced; only the title is truncated, at a UTF-8 character boundary, so the whole name fits in 220 bytes and the ID tag and suffix always survive. A title with nothing left to keep fails the item. Playlist folder names follow the same rule with the playlist title and ID.
- The source revision's reuse of legacy title-only files is dropped. The collector only ever looks for and writes ID-tagged names.
- A video that appears in several playlists, or both in a playlist and at the root, is downloaded separately into each location. Duplication is accepted.
- Every write uses the `NEVER` policy, checked per artifact before any network work: an existing MP4 is not downloaded again, and an existing SRT is not fetched again. Each artifact is judged independently, so a job can skip an existing MP4 and still fetch its missing SRT.
- An existing artifact is recognized by its video ID within the destination directory, as a file named `<anything> [videoID].mp4` or `<anything> [videoID].srt`, whatever its title part says (decision C23). A video renamed on YouTube therefore still counts as existing, and the check needs only the ID, which the URL or the persisted playlist entry supplies. Matching treats the ID literally, not as a glob pattern. Any match counts as existing, even when several files carry the same ID and suffix. A new SRT takes the stem of the existing MP4 when exactly one exists, so the pair stays together after a title change; otherwise it uses the current title (decision C33).
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

Item states are `queued`, `running`, `done` and `failed`. A failure in one item never stops the remaining items of its job (decision D17 at the source revision). Job states are `queued`, `expanding`, `running` and `finished`; a job whose playlist expansion fails becomes `finished` with no items and a failure record.

### 5.8 Status table and reconciliation with staging

The status table holds jobs and items. Lifetime rules:

| Record | Kept until |
|---|---|
| Item that is queued or running | It finishes |
| Item that is done or failed | None of the files it produced or found existing remains in staging |
| Job | It is `finished`, has no items left, and no uncleared failure refers to it |

Only files the collector itself wrote or found as its own artifact count. Other files beside them, such as an SRT written by `youtube-whisperer`'s transcriber, do not keep a record alive. An item that produced no files, such as a `captions` item with `unavailable` captions or a failed item, is removed at the next reconciliation; its failure record (5.9) survives independently. A job that is `queued`, `expanding` or `running` is never removed, even before it has any items; the `finished` condition exists because a playlist job has no items until expansion persists them (decision C26).

Because SMB mounts do not deliver file-change notifications, the collector MUST reconcile by checking file existence: when `GET /jobs` is requested, and periodically on a separate thread from the YouTube worker. With 10 to 20 files in staging this costs a few dozen metadata calls. A stalled SMB share can block a metadata call indefinitely, and a thread timeout does not cancel blocked I/O, so API request handling MUST NOT perform staging filesystem calls itself and MUST stay responsive while the share is stalled (decision C25). `GET /jobs` therefore asks the reconciliation thread for a pass and returns the status table together with the time of the last completed reconciliation; at most one staging probe is outstanding at a time, so repeated requests cannot accumulate blocked threads. How long `GET /jobs` waits for a requested pass before returning is an implementation default, not part of the contract (decision C35).

Reconciliation MUST NOT delete records when the staging area itself is unavailable; see 5.13.

### 5.9 Failures

Failures are their own resource, separate from items, and carry everything needed to understand them after their item or job is gone:

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

A failure stays until the user clears it, or until it is **superseded**: a later item for the same video and the same artifact reaches a definitive result. For media that means `downloaded` or `existing`. For captions it means `downloaded`, `existing` or `unavailable`. A playlist expansion failure is superseded by a later successful expansion of the same playlist. The orchestrator reads failures but does not clear them.

### 5.10 Change feed and long polling

Every change to a job, item or failure, including removal, MUST stamp the record with the next revision. Removal leaves a **tombstone** holding the record's final representation, so a caller that was offline still learns how an item ended before the user moved its files away.

- `GET /jobs/changes?since=<cursor>&wait=<seconds>` returns the changes with revisions greater than `since`, oldest first and coalesced to the latest state of each record. It returns immediately when changes exist; otherwise it waits up to `wait` seconds and then returns an empty page. The response carries the next cursor and whether more changes remain.
- `GET /jobs/{id}?since=<revision>&wait=<seconds>` waits the same way for one job.
- `wait` MUST be capped (assumed 60 seconds) so proxies and clients do not cut off idle requests.
- Tombstones are kept for a configured period (assumed 7 days). Pruning records the highest pruned revision `P` durably; a request with `since < P` MUST return 410, telling the caller to resynchronize from `GET /jobs`, while `since >= P` has lost nothing (decision C27). How a resynchronizing caller obtains a consistent snapshot and cursor, including failures, and how pagination interacts with coalescing are open (section 12).
- A running item's progress reports downloaded bytes, plus total bytes and a percentage whenever yt-dlp knows the total size.
- Download progress updates MUST be throttled (assumed at most once a second per item, plus every state change), so waiting callers are not woken by every progress tick.

SQLite is the source of truth. An in-memory notification only wakes waiting requests, which then read the database again; the worker thread signals the event loop thread-safely. This requires the API and worker to run in one process.

### 5.11 Cookies

Cookie handling stays as at the source revision: yt-dlp uses Firefox cookies when a `cookies.sqlite` file is found under the standard Firefox profile locations, and the Compose file mounts the profile from `FIREFOX_PROFILE_DIR`. Detection happens once per process. The profile holds live session cookies; agents and tests MUST NOT read or copy it.

### 5.12 Restart recovery

On startup, items left `running` return to `queued`, and a job interrupted during expansion expands again. Because every artifact is checked with `NEVER` before work, repeating an item that had already published its MP4 skips the download. A partial download is never resumed; it restarts in a fresh stage directory.

On SIGTERM the worker stops taking items and lets the current yt-dlp call finish, abandoning it when the shutdown grace period ends; the interrupted item is then recovered by the rule above on the next start (decision C36). The grace period is an implementation default.

### 5.13 Staging availability

An unmounted SMB share can look like an empty directory. Reconciliation would then see every file as removed and delete every record, and downloads would be written into the container instead of the share. The staging root MUST therefore contain a marker file, `.youtube-collector-root`, which the user creates once.

- Before reconciling, and before starting each item, the collector MUST check that the marker exists.
- While the marker is missing or the staging root cannot be read, the worker and reconciliation MUST pause without changing any record: no items start, fail or get removed. Both resume on their own once the marker is visible again. An item already running when the share disappears fails with category `filesystem`, as any write failure does.
- A stalled share, where filesystem calls block instead of failing, is accepted to stall the YouTube worker or the reconciliation thread for as long as the stall lasts; it MUST NOT stall the API (5.8, decision C25).
- `GET /health` reports the paused state (5.14), and the collector logs a warning when it pauses and when it resumes.
- The collector never creates the marker itself, because creating it on an unmounted mount point would defeat the check.

### 5.14 Health

`GET /health` MUST report, without contacting YouTube:

- whether the database is readable and writable
- whether the staging marker is present, and therefore whether work is paused
- whether the worker and reconciliation threads are alive
- the installed yt-dlp version

It returns 200 when everything is healthy and 503 otherwise, with the same body in both cases. The staging result is the most recent cached check, reported with its age, so `/health` itself never touches the share and stays responsive during an SMB stall (decision C25). The age beyond which a cached result counts as unhealthy is an implementation default, not part of the contract (decision C35).

## 6. HTTP API summary

| Endpoint | Purpose |
|---|---|
| `POST /jobs/videos` | Submit a media job |
| `POST /jobs/captions` | Submit a captions job |
| `POST /jobs/videos-with-captions` | Submit a media-and-captions job |
| `GET /jobs` | The status table: every job with its items, after requesting a reconciliation pass, with the time of the last completed one (5.8) |
| `GET /jobs/{id}` | One job; optional `since` and `wait` for long polling |
| `GET /jobs/changes` | The change feed; `since` and `wait` |
| `GET /failures` | All uncleared failures, oldest first |
| `DELETE /failures` | Clear all failures; returns the cleared records |
| `DELETE /failures/{id}` | Clear one failure |
| `GET /health` | Service health and paused state (5.14) |

An unknown job or failure returns 404. Validation errors return 422. Unexpected errors return a generic 500 and are logged, as at the source revision. Exact JSON schemas are left to implementation and are published through FastAPI's OpenAPI document.

## 7. Deployment and operation

- One Docker Compose service runs one process: the HTTP server, the single YouTube worker thread and the reconciliation thread. It MUST NOT run several server processes.
- The SQLite database MUST live on a Docker named volume, never in `ASSETS_DIR`. Container storage is local, unlike the SMB-mounted staging area where SQLite locking is unreliable, and a named volume survives container recreation, including the image rebuilds that update yt-dlp. SQLite SHOULD run in WAL mode.
- `ASSETS_DIR` is bind-mounted as the staging area, and `FIREFOX_PROFILE_DIR` as the cookie profile, as in `youtube-whisperer`'s Compose file.
- The service is unauthenticated and LAN-only, as decision D5 states for `youtube-whisperer`. It MUST NOT be exposed to the public internet.
- Dependencies are locked with uv and installed through the shared `docker-build-common` helpers, as in `youtube-whisperer` (D50, D52). yt-dlp breaks whenever YouTube changes and ships fixes often, so the project MUST provide a script that runs `uv lock --upgrade-package yt-dlp` and applies the new lock to the devcontainer's venv, following `youtube-whisperer`'s `update-dependencies.sh`. Rebuilding the image is a separate step run on the host, because the devcontainer has no Docker (decision C29, superseding C19's "rebuilds the image"). Other dependencies keep their locked versions.
- `pyproject.toml` declares no upper version bounds. `uv.lock` makes builds reproducible, and every full upgrade's `uv.lock` diff is reviewed before commit (decision C32).
- The image is built from `ubuntu:26.04`, whose distro `python3` is Python 3.14, keeping the distro-Python and `ubuntu`-user design (decision C22, superseding T1's `ubuntu:24.04` and Python 3.12).
- The devcontainer MUST NOT inherit the production mounts (decision C28). Its default configuration replaces them with development-only storage: a throwaway staging volume, its own database volume and no browser profile, whatever `.env` sets. A second, explicitly named configuration (`.devcontainer/real-data/`, labelled "REAL DATA") adds the real staging share and a read-only Firefox profile back for sessions the user deliberately opens; it fails to start unless `ASSETS_DIR` and `FIREFOX_PROFILE_DIR` are set, sets `COLLECTOR_REAL_DATA=1`, and keeps the development database, because a development collector and the production service sharing one SQLite file could corrupt each other's records. Opening it authorizes nothing: each run against real YouTube still needs the user's authorization for that run, and tests still use only test-owned storage. The Compose service has no fixed `container_name`, one prerequisite for the devcontainer and the production service to coexist on one Docker daemon; they also need distinct Compose project names, which [TODO](TODO.md) T13 tracks.
- The production Compose service mounts the Firefox profile read-only; yt-dlp copies `cookies.sqlite` to a temporary directory before opening it (verified in the locked yt-dlp source on 2026-11-07; the mount itself is unverified).
- The devcontainer uses VS Code's SSH agent forwarding instead of bind-mounting `~/.ssh`, so private key files are not present in the container; processes in it can still use the forwarded agent while it is connected (decision C31).
- The user creates the staging marker (5.13) once, at the root of the share.
- Each thread owns its own SQLite connection, and every write is one short transaction that also assigns the revision (decision C36).

Implemented by T1, T2, T3, T4, T5 and T8 ([records](completed_work/)): a Dockerfile with four stages from a digest-pinned `ubuntu:26.04`, using the shared helpers. `base` holds ca-certificates, ffmpeg, distro Python 3.14 and Deno copied from a digest-pinned `denoland/deno` binary image; `build` adds git and uv, checks `uv.lock` against `pyproject.toml` offline, and builds the locked venv at `/opt/venv` with the project as a regular package; `runtime` is `base` plus that venv, without git, uv, curl or the source tree; `dev` continues from `build` with curl, the dev group and an editable project. The single Compose service `app` has the staging, read-only Firefox-profile and `collector_db` mounts (the database directory is `/var/lib/youtube-collector`) and no fixed container name. The devcontainer replaces those mounts with development-only volumes, offers the opt-in real-data configuration, pins its feature to a major version with a committed lock, and its creation hook checks the lock offline and records GitHub's SSH host keys. `./update-dependencies.sh yt-dlp` fulfils the yt-dlp update as C29 restates it; rebuilding the image on the host deploys it. [TODO](TODO.md) tracks the remaining review tasks. The service entrypoint and published port are added with the API. [README](README.md) owns setup and configuration.

Configuration (environment variable names are to be chosen during implementation):

| Setting | Assumed default |
|---|---|
| Staging directory inside the container | required |
| Database path | a file on the named volume |
| YouTube request pacing | yt-dlp's defaults (no extra sleep) |
| Reconciliation interval | 30 seconds |
| Long-poll maximum wait | 60 seconds |
| Tombstone retention | 7 days |

## 8. Behavior carried over and dropped

Carried over from `youtube-whisperer` at the source revision, reimplemented here:

| Behavior | Source |
|---|---|
| `Title [id].ext` naming with 220-byte truncation | D47; `downloaders/utils.py` |
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
| Reuse of legacy title-only files (part of D47) | Dropped |
| Enqueuing transcription follow-ups and the `none` transcriber | Dropped; the orchestrator decides |
| Channels and channel tabs | Rejected |
| Filesystem glob tasks, `GET /assets`, `DELETE /assets` | Not part of the collector |
| Completion inferred only from files on disk | Status table plus reconciliation with staging |

## 9. Engineering standards

The repository adopts `youtube-whisperer`'s working standards: its AGENTS.md, the adversarial review and grouped task execution skills, the coverage gate, mypy and ruff, adapted on 2026-11-06 from `youtube-whisperer` revision `474f5dc`. The rationale for those standards is recorded in that revision's `DESIGN.md` under "Engineering governance" (decisions D1, D4 and D28–D44) and is not copied here. Adaptations: references to Redis, Azure and Whisper were replaced with the collector's own resources, the test-import rule names `youtube_collector`, and running anything against real YouTube needs the user's authorization for each run. Automated tests MUST NOT contact YouTube or read the Firefox profile, and MUST keep all reads and writes inside test-owned temporary directories, including the SQLite database. The worker thread, the reconciliation thread and long-poll waiters MUST be testable with injected clocks and explicit completion signals rather than elapsed-time waits.

## 10. Integration with youtube-whisperer

Switching the orchestrator over is a later, separate phase. The collector SHOULD first run alongside the existing system, proving itself before anything depends on it. The intended use:

1. The orchestrator submits a `videos-with-captions` job with an `idempotency_key` it has recorded beforehand, so a crash between submitting and recording the job ID cannot create a duplicate.
2. It follows `GET /jobs/changes` from a persisted cursor.
3. For each finished item whose captions are `unavailable` or `error`, it maps the item's staging path to its own mount of the same directory and requests transcription. The decision moves out of `YouTubeWorker.process_video`.
4. It treats a missing job (404) as "nothing left to do", and failures as information to record. It does not clear them. A tombstone carries a final outcome: the orchestrator records that outcome and any failures it reports, but never requests transcription from a tombstone, because the item's files have already left staging (decision C30).

Paths in responses are relative to the staging root, so the collector and orchestrator may mount the staging area at different container paths.

## 11. Decisions agreed in discussion (2026-11-06)

| ID | Decision |
|---|---|
| C1 | The collector is its own Dockerized service with its own work queue, superseding `TRANSCRIPTION_REDESIGN.md`'s recommendation of one orchestrator-owned queue. The queue is SQLite. |
| C2 | One YouTube connection at a time, from a single thread. |
| C3 | Write into the existing `ASSETS_DIR`, a staging area from which the user removes files. |
| C4 | Playlists get their own `Title [playlistID]` folder; duplicates across playlists are accepted. |
| C5 | Polling only, with long polling. No webhooks or Server-Sent Events. |
| C6 | No CLI, superseding the original idea of offering one. |
| C7 | LAN-only and unauthenticated. |
| C8 | The status table lists pending work and completed work whose files remain in staging, refreshed by reconciliation; there are endpoints to view the status table and to clear failures. |
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
| C20 | Progress reports bytes, and a percentage when the total is known. |
| C21 | YouTube Mix playlists are rejected. |

Decisions from the project review (2026-11-07), made by the user after two independent reviews of revision `ca869a8`:

| ID | Decision |
|---|---|
| C22 | Build from `ubuntu:26.04` with its distro Python 3.14, rather than `python:3.14-slim`, to keep the distro-Python and `ubuntu`-user design. Supersedes T1's `ubuntu:24.04` and Python 3.12 direction. |
| C23 | Recognize an existing artifact by video ID within its destination directory, including after the video's title changes. |
| C24 | Retain idempotency keys for at least seven days after their job is removed. |
| C25 | Keep the API responsive during SMB stalls: no staging filesystem calls on the API path, cached staging health reported with its age, and a stalled worker or reconciliation thread accepted. |
| C26 | Reconciliation removes only `finished` jobs. |
| C27 | 410 applies when `since` is below a durably recorded pruned-through revision. |
| C28 | The devcontainer uses development-only storage by default; a separate, explicitly named real-data configuration mounts the real share and a read-only Firefox profile, never the production database (section 7). The fixed Compose `container_name` is removed. |
| C29 | Supersedes C19: the update script updates `uv.lock` and the devcontainer's venv; rebuilding the image is a separate host step. |
| C30 | The orchestrator records the outcome and failures carried by a tombstone but never requests transcription from it. |
| C31 | The devcontainer uses SSH agent forwarding instead of a `~/.ssh` bind mount. |
| C32 | No upper bounds on dependencies; rely on `uv.lock` and review the lock diff on every full upgrade. |

C26 and C27 correct contradictions that both reviews found, rather than choosing between alternatives.

Decisions on the review's open questions (2026-11-07), made by the user. Q3 remains open (section 12).

| ID | Decision |
|---|---|
| C33 | Settles Q1 (refines C23): any file matching the video ID and suffix counts as existing, even when several do. A new SRT takes the stem of the existing MP4 when exactly one exists; otherwise it uses the current title (5.4). |
| C34 | Settles Q2 (refines C24): the retained idempotency key stores the job's final representation, which a replay after removal returns with 200; a different body under a retained key returns 409; an expired key is forgotten and its reuse creates a new job (5.1). |
| C35 | Settles Q4: C25 stands unchanged. How long `GET /jobs` waits for a requested reconciliation, and the age at which cached staging health counts as unhealthy, are implementation defaults rather than contract details, so they block no task (5.8, 5.14). |
| C36 | Settles Q5: on SIGTERM the worker stops taking items, lets the current yt-dlp call finish or abandons it at the grace period, and relies on restart recovery; each thread owns its SQLite connection, and every write is one short transaction that also assigns the revision (5.12, section 7). |
| C37 | Settles Q6: playlists are not capped in size, and the API does not validate the `Host` header against DNS rebinding. Neither is a demonstrated defect, and the service stays LAN-only (C7). |

## 12. Open questions

These contract details were raised in the 2026-11-07 review and MUST be settled before the application tasks that depend on them. Each lists a proposal, which is not accepted. Q1, Q2 and Q4–Q6 were settled as C33–C37 (section 11).

| ID | Question | Proposal |
|---|---|---|
| Q3 | Change-feed recovery (C27): how does a caller resynchronize consistently, including failures, and how do pages coalesce? | `GET /jobs` and `GET /failures` each return the revision their snapshot was read at, from one transaction. A change page lists each changed record once, at its latest revision, ordered by that revision; the next cursor is the last revision on the page. A record that changes again reappears on a later page. |

## 13. Assumptions

These follow from repository evidence or the discussion, and stand unless the user objects:

- The API and worker share one uvicorn process, as required by in-process long-poll notification.
- The tuning values in section 7 are starting defaults, not measured requirements.
- An item's title and ID come from a metadata request on the worker thread, as at the source revision; the implementation MAY use a playlist's flat-extraction entries instead, when they supply the same values.
- The collector writes only new files and never deletes anything from staging.
