# Full-project review — 2026-11-10

## Boundary and evidence

Requested assessment of architecture, correctness, maintainability, testing and operations at revision `d565907039b7550f550d860f3d4fdd2891ebc8d4`. Scope includes all application modules, design and operating contracts, test structure and representative integration/failure tests, dependency and coverage tooling, Docker and devcontainer configuration. This is a review, not authorization to implement proposals or supersede DESIGN decisions. Application code and configuration remain unchanged. The adversarial-review skill was not used, and this is not its formal pre-commit verdict.

Assumptions: the single-user, trusted-LAN, few-jobs-per-day workload and one collector process remain as recorded in [DESIGN](DESIGN.md). Those assumptions justify keeping SQLite, polling and one worker. The unrelated untracked `setup-wsl-ssh-agent.sh` was excluded. No real staging data, browser profile or production database was inspected; no YouTube traffic or service deployment was performed.

Validation ran in the Docker development container on Python 3.14.4: `./check-coverage.sh -q` passed with 612 tests and 100% measured statement and branch coverage; `mypy youtube_collector` passed for 13 source files; `ruff check .` passed. The existing gate includes tests of review tooling; running that suite did not invoke the excluded review procedure. Two additional temporary-directory probes are described below. Their injected faults establish control-flow behavior, not the frequency of production storage failures. Temporary databases and files were removed on context exit. No background process was started by the probes.

## Overall assessment

The architecture fits the workload. Separating API database operations from staging I/O is valuable, the worker owns network scheduling, transactions are short in normal operation, and staging publication has a clear owner. Thin library adapters, explicit clocks and real temporary SQLite databases make the application testable without an elaborate framework. Polling, submission-triggered retention and the absence of reconciliation are useful simplifications already made in C39–C46. There is no compelling reason to add a broker, ORM, async download stack, plugin framework or more services.

The main weakness is fault handling across boundaries: surviving an exception does not necessarily restore useful progress, and finishing a Python shutdown method does not necessarily mean all resources have stopped. Coverage is excellent, but the two reproduced gaps show why execution coverage alone cannot certify those properties.

## Findings

### F1 — High: persistence failure can strand work while the worker looks healthy

**Evidence: reproduced.** [Worker.run](youtube_collector/worker.py) catches an unexpected step exception, logs it and continues. `_process` has already persisted `running` before `finish_item` saves its outcome. If that save fails, [Store.next_work](youtube_collector/store.py) selects only queued work, so the interrupted item is never selected again until startup recovery runs. Failed persistence after playlist expansion has the analogous problem. An unfinished job also never expires.

Probe: submit one video into a temporary database; use a fake media adapter that writes a small local file; inject one `sqlite3.OperationalError` from `finish_item`; restore the real method and call `step` again. Observed: the MP4 exists, the next step is `idle`, and both job and item remain `running`. `Store.check()` succeeds and the worker refreshes a present staging marker. Explicit `recover()` followed by another step finishes the job. From the health implementation, a live run loop in this state can therefore return healthy despite the stranded job; the probe did not launch an HTTP server.

This behavior is acknowledged in the worker docstring as recovery at the next restart, but the operational consequence is easy to miss: nothing schedules that restart and the ordinary health signals need not expose the problem. `test_run_survives_a_failing_step` replaces the whole step with a fake and proves loop survival, not job recovery.

**Recommendation:** give persistence/infrastructure failures an explicit recovery policy. Prefer retrying persistence of the already-computed outcome after restoring a usable connection, or stopping consumption and exposing a durable/observable unhealthy condition requiring restart. Requeuing immediately is another option, but can repeat acquisition if the user has moved a published file. Do not silently add general retry of failed YouTube jobs, which DESIGN excludes.

**Trade-off:** automatic infrastructure recovery costs additional state handling; operator restart is simpler but must be visible. User disposition: automatic recovery is desired but not urgent (DESIGN C48); [T35](tasks/open/T35.md) records the work. No behavior changed.

### F2 — Medium: failed transaction finalization can poison a long-lived connection

**Evidence: reproduced with an injected SQLite COMMIT denial.** In [Store._write](youtube_collector/store.py), `COMMIT` is outside the exception/rollback block. `_read` likewise has no cleanup if its final `COMMIT` fails. The same connection is reused for the worker lifetime or cached on the API database thread.

Probe: on a temporary initialized Store, install a SQLite authorizer that returns `SQLITE_DENY` for exactly one `SQLITE_TRANSACTION` action whose first argument is `COMMIT`, then allows all later actions. `Store.check()` raises `not authorized`; `connection.in_transaction` remains true; the next `Store.check()` raises `cannot start a transaction within a transaction`. Explicit rollback restores operation. This demonstrates the missing exception cleanup; it does not claim that every disk error leaves SQLite in this state.

**Recommendation:** cover transaction finalization in the exception-safety contract, inspect whether a transaction remains active, and roll back or discard an unusable connection while preserving the original error. Account for SQLite failures that have already rolled back automatically. Test body failure, commit failure and cleanup failure separately. Fixing F1 without this can leave recovery trying repeatedly on a poisoned connection.

Disposition: [T36](tasks/closed/T36.md) records the proposed focused reliability fix; not implemented.

### F3 — Medium: shutdown has an unbounded API-database tail

**Evidence: source inspection; not a reproduced Docker shutdown overrun.** [_Runtime.stop](youtube_collector/api.py) bounds the worker join, then calls `executor.submit(close).result()` and `executor.shutdown(wait=True)` without a bound. A running database operation can outlast that wait; cancelling an awaiting HTTP coroutine does not stop the underlying operation. SQLite's configured busy timeout is 30 seconds, and that is not a universal bound on local filesystem I/O or query execution.

Consequently the [Compose](docker-compose.yaml) comment that 45 seconds prevents SIGKILL from cutting shutdown short is stronger than the implementation guarantees. The existing abandonment test exercises a slow media adapter and subsequently releases and joins it; it does not exercise a blocked API database executor or actual process exit.

**Recommendation:** add a subprocess-level shutdown test with owned temporary storage and controlled blocking. Establish one overall shutdown budget, including the API database work, and document Docker's final kill as a fallback. Merely adding a timeout to `result()` does not terminate an executor thread. A fully bounded in-process shutdown may require a larger ownership change than the service warrants.

**Accepted disposition (C50):** retain graceful shutdown for normal operation and accept container-enforced termination for exceptional stalls. [T37](tasks/closed/T37.md) records the documentation corrections and process-level shutdown/restart tests needed to validate that policy. Stronger application-enforced termination is not required.

### F4 — Low: caption HTTP session ownership is implicit

**Evidence: source inspection, including the installed dependency.** [CaptionClient](youtube_collector/captions.py) creates a requests Session and passes it to `YouTubeTranscriptApi`, but exposes no close operation. The installed transcript client has no `close`, context-manager or destructor cleanup API. The runtime owns the adapter but never explicitly closes its session.

For one process-lifetime client, process exit limits the practical cost. Repeated application lifespans or future adapter replacement would make resource lifetime less predictable. Retain explicit ownership of the default session and close it when the worker actually stops; do not close it concurrently with an abandoned worker still using it. This is housekeeping, not a reason to redesign the adapters.

Disposition: [T38](tasks/open/T38.md) records the focused cleanup requested by the user.

## Significant deployment and product trade-offs

### D1 — Validate SMB publication before investing in further architecture

[Staging.publish](youtube_collector/staging.py) delegates to the pinned `pathlib-extensions` implementation. The installed implementation resolves the target, stages beside it and publishes under NEVER; [T25](tasks/open/T25.md) already records that this publication uses a hard link without a fallback and has not been verified on the real SMB share. A successful ext4 test does not establish deployment compatibility. If the server/mount does not support the operation, every new artifact can fail publication.

The review recommends treating this as a deployment prerequisite. The user directed that hard-link testing stay in [T25](tasks/open/T25.md); no separate task or priority change was made. An explicitly authorized synthetic filesystem check on the intended share need not involve YouTube or cookies. If hard links are unavailable, choose between changing the mount/backend and implementing a different no-overwrite publication mechanism. A plain existence-check followed by rename or copy is not an equivalent guarantee. No implementation change is justified until the actual share behavior is known. Built-image checks remain under [T10](tasks/open/T10.md).

### D2 — Media authentication and caption authentication are different capabilities

[default_components](youtube_collector/api.py) passes detected Firefox-cookie availability to yt-dlp only. [CaptionClient](youtube_collector/captions.py) uses its own unauthenticated session. The installed transcript library's constructor also explicitly comments that its cookie authentication is disabled. Cookie-enabled media therefore does not imply cookie-enabled captions; configuring Firefox cookies is not a general remedy for caption authentication failures. This is an architectural capability limit, not a demonstrated failure on a particular video.

If public captions are sufficient, keep both adapters and make this limit explicit in operating guidance. If captions for authenticated/private content matter, first establish the requirement and validate the backend options, then consider using yt-dlp for that acquisition. Unifying adapters would change language discovery, generated/manual precedence, translation exclusion, timeouts, SRT conversion and error mapping, so it is not a mechanical dependency cleanup. Likewise, configured pacing currently belongs to yt-dlp; it is not a shared throttle for the separate caption client.

Disposition: the user identified unavailable cookies as an issue to fix; DESIGN C49 records the desired capability and [T39](tasks/open/T39.md) records the open task. Backend choice remains unresolved; no implementation changed.

### D3 — Deployment trust boundaries are operational, not enforced isolation

Compose publishes `${COLLECTOR_PORT}:8000` without a host-interface restriction, while the service has no authentication. C7 and C37 explicitly accept the trusted-LAN model and absence of Host validation. Preserve those decisions; verify the host's actual exposure during deployment. An optional bind-address setting could make the intended interface explicit, without adding authentication machinery.

Similarly, staging intentionally follows symlinks and reports lexical paths. A staging-relative response is not a proof that the physical target stays under the root. Existing tests and T17 explicitly preserve symlink behavior. This is acceptable only with a trusted staging tree; reject or constrain symlinks only if that trust assumption changes, not as an unsolicited security redesign.

### D4 — Unbounded list responses are acceptable at current scale

[Store.list_jobs](youtube_collector/store.py) loads all retained jobs, then queries each job's items and failures; the API serializes the complete result. Failures can be retained indefinitely. Large lists occupy the single API database executor and consume memory, and a large playlist remains one expansion unit before round-robin item scheduling begins. These are scaling limits, not demonstrated defects for a few jobs a day. C37 explicitly accepts uncapped playlists.

Do not add pagination, admission control or incremental expansion preemptively. Revisit when actual list latency, retained data size or expansion duration affects use. An orchestrator should poll its own job rather than repeatedly fetch the whole status table, as DESIGN already prescribes.

## Testing improvements with the highest return

1. Add the F1 and F2 fault scenarios as behavioral regression tests: after the fault clears, assert the chosen recovery outcome, usable connections and observable health. Cover playlist result persistence as well as item completion. Keep tests on real temporary SQLite databases, injecting only the failure boundary.
2. Add an offline subprocess lifecycle test: start a test-configured application with fake acquisition, interrupt it at controlled points, restart against its owned database and verify recovery and no overwrite. Include publication-before-outcome persistence and a blocked API database operation. Own and reap the subprocess on every path. Do not rely solely on TestClient for process-exit guarantees.
3. Extend adapter contract tests. The existing real yt-dlp loopback test is useful, but downloads synthetic bytes as one combined format; it does not exercise a real audio/video merge. Serve tiny generated media tracks locally and run the actual merge path. For captions, feed owned fixture responses through the actual transcript client/parser, covering malformed payloads, generated tracks and no matching track; current fake tracks mainly test selection and formatting boundaries.
4. Enforce offline test execution rather than relying only on correct mocking. The shared conftest owns threads and loopback servers but has no suite-wide external-network deny guard. A narrowly scoped guard should allow owned loopback traffic and fail unexpected external connections; subprocess tests need equivalent isolation. This is a preventative safeguard, not evidence that the current suite contacted YouTube.
5. Prefer these cross-boundary tests over adding more assertion-per-line tests merely to retain 100% coverage. Random ordering, real temporary databases, controlled clocks and explicit thread teardown are already strong practices and should remain. A checked-in CI workflow is absent; automated execution of the existing gates would help if this project gains more contributors, but is not a prerequisite for the current personal workflow.

## Simplification opportunities and things to retain

- Remove the duplicate structural track protocol: `worker.Track` and `languages.CaptionTrack` declare the same two properties. Share the small contract in one neutral place or import the existing protocol if that dependency direction remains clear. Keep separate media and caption adapters until D2 is settled.
- There is a typing gap between Store's `dict[str, object]` records and the Pydantic response models. Response validation is useful and should stay. If response changes become frequent, use a small typed record boundary or move serialization to one owner; do not add an ORM or another whole model hierarchy merely for theoretical layering purity. Today the store-to-HTTP coupling is a reasonable small-application trade-off.
- Keep the dedicated API database thread. Replacing it with direct synchronous database calls inside async routes would lose event-loop responsiveness, while removing `check_same_thread` would weaken an ownership guarantee. Thread-local connection storage has a concrete purpose here.
- Keep explicit outcome enums and the distinction between job completion and artifact success. These encode real product semantics, including partial caption failure, rather than gratuitous state-machine abstraction.
- Correct the `get_job` route docstring's claim that a job past retention returns 404: C42 deliberately leaves it readable until a subsequent submission prunes it. This is documentation drift, not a runtime defect.
- The governance and tooling footprint is substantial for a small service. Consider retiring obsolete historical implementation detail from everyday guidance only when it obscures current instructions; do not weaken isolation, review independence, mutation evidence or durable decision records as a casual cleanup. No governance change is proposed here.

## Suggested order and disposition

Original review recommendation: prioritize F1/F2 and the SMB deployment check, then boundary-focused offline tests and the shutdown contract. User disposition: automatic recovery is desired but non-urgent; F1–F4 now have open tasks T35–T38, unavailable caption cookies have T39, and hard-link testing stays in T25. Take the small maintenance cleanups only as separate scoped work. Pagination, additional services and broad abstraction changes can wait for evidence of need.

The initial assessment changed only this report. The follow-up records five open tasks and DESIGN decisions C48–C50; the user requested this report at the project root and authorized committing the documentation. C50 accepts container-enforced termination for exceptional stalls; no runtime fixes have been implemented. References and diffs were checked, and AGENTS.md remained unchanged. Real SMB behavior, cookie-authenticated acquisition, Docker process shutdown and the remaining T10/T25 host checks were not validated by this review.
