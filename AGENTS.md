This file owns repository-wide working principles and the standards every change must meet. All agents MUST follow them and explicit user instructions; higher-priority platform instructions prevail. CAPITALIZED requirement words have the meanings defined by BCP 14 (RFC 2119 and RFC 8174).

# Document Boundaries

- **AGENTS.md:** working principles and required standards.
- **[DESIGN.md](DESIGN.md):** product requirements, architecture, product contracts, design rationale and accepted user decisions.
- **[README.md](README.md):** project overview, and build, run, configuration and test procedures, including how the gates run.
- **[tests/README.md](tests/README.md):** conventions for writing tests.
- **[tasks/](tasks/):** task state and completion records; [tasks/README.md](tasks/README.md) owns its rules.
- **[Adversarial review skill](.agents/skills/adversarial-review/SKILL.md):** how review is conducted, including mutation evidence and evidence retention.
- **[Grouped task execution skill](.agents/skills/grouped-task-execution/SKILL.md):** the recommended workflow for a group of related tasks, unless the user chooses another.

Link to the owning document instead of duplicating its procedure. Skills and operational documents implement these standards and do not override them, including the safety and ownership rules here. Only DESIGN's recorded decisions authorize product changes; proposals in tasks or reviews do not. If documents disagree, follow the owning document, and confirm any unresolved material conflict before dependent work.

# Scope and Decisions

- If not running in a Docker container, stop and confirm with the user before continuing. The repository assumes its own devcontainer (`.devcontainer/`) with harness permissions set to Full Access or bypass; under a more restrictive harness, follow its enforced policy and report the mismatch.
- Read the relevant code and documentation, and inspect repository status, before editing.
- Each change MUST have a boundary: intent, dependencies, non-goals, validation and done criteria. Revalidate written tasks against the code; do not implement them mechanically or broaden their material scope unilaterally.
- State assumptions. Confirm unresolved choices that materially affect scope, architecture, dataflow, correctness, security or user-visible behavior before dependent work, and continue independent work meanwhile. Non-material assumptions MAY be made when repository evidence supports them and they preserve the requested outcome; name each, with its evidence and effect, in the handoff.
- Accepted decisions and user authorization persist; do not ask again about settled choices. Record a superseding decision and its reasoning before committing the affected change.
- The project is developed on several machines, which share only the Git repository. Boundaries, decisions, open review findings and completion evidence MUST live in committed files. Working records, session scratch, agent memory and tool history are machine-local: committed files MUST NOT refer to them by path, and anything every machine needs is promoted into the document that owns it.

# Design and Documentation

- Prefer the simplest cohesive implementation within the assigned scope. Give each function, class and module a clear responsibility; prefer pure logic, isolated side effects and minimal mocking over unnecessary abstractions.
- Respect lint limits without splitting cohesive code solely for length. A narrow exception explains the responsibility or invariant it preserves; avoid blanket disables, `Any` and `type: ignore`.
- Before removing a layer, identify all unique behavior it carries, including copy, errors, ordering, timing, diagnostics and API-visible behavior, and give each retained behavior an explicit owner and verification.
- Verify assumptions about languages, frameworks, libraries and services empirically or against version-appropriate documentation, and verify empirical claims before recording them. Historical evidence identifies its revision, environment, provenance and limitations, and does not certify current validation.
- Update documentation that the change makes stale. Distinguish current behavior, accepted decisions, proposals and unverified hypotheses.
- Markdown documentation MUST use one source line per prose paragraph; keep the line breaks Markdown structure needs, such as headings, lists, tables, blockquotes and fenced code.
- New or changed non-test functions, methods and classes MUST have docstrings covering their purpose and any non-obvious contracts, side effects or constraints. Comments explain non-obvious reasons or trade-offs.

# Tests

- Tests MUST protect a behavior or invariant and fail when it breaks. Prefer controlled inputs, injected clocks and explicit completion signals; bounded wall-time checks are allowed only where time alone can falsify the property. [tests/README.md](tests/README.md) owns the conventions.
- Automated tests MUST NOT reach external services such as YouTube, through any client. Every read and write by a test or the code under test stays in test-owned temporary directories, on failure paths too, including each test's own SQLite database; environment variables, settings and other process state a test changes are test-owned and restored. Reading source, installed dependencies and static fixtures is permitted; it is not permission to access host data or real configuration.
- Background work a test starts, directly or through the code under test, MUST be stopped and accounted for before the test ends, also after failures and timeouts. Tests run in random order, so order-dependent state is a defect.
- High-impact invariants, such as protection against data loss, security violations, incorrect acknowledgement and concurrency or resource-ownership failures, MUST have targeted mutation evidence from an isolated scratch copy that preserves test ownership. Before removing or weakening coverage, show that a retained test still fails when the protected property breaks. The [review skill](.agents/skills/adversarial-review/SKILL.md#evidence-retention) and its [scratch-testing recipe](.agents/skills/adversarial-review/references/scratch-testing.md) own the method.
- `check-coverage.sh` owns the coverage policy; statement and branch coverage MUST each reach its threshold for every measured file and for the project. Changes to the policy are planned as tasks.

# Validation and Review

- Before implementing, establish a passing baseline for the relevant checks on the starting state, using the full gates when the scope is broad or uncertain. Fix a failing baseline in a separate, authorized change first.
- A change is non-trivial if it could affect runtime behavior, test guarantees, build or configuration output, security, concurrency, state or data flow, or user-visible behavior. The final candidate of every non-trivial change, configuration-only ones included, MUST pass `./check-coverage.sh`, `mypy youtube_collector` and `ruff check .` in the tree being validated, as [README Testing](README.md#testing) describes. Resolve new warnings and explain accepted tooling warnings.
- Every non-trivial change MUST pass the [review procedure](.agents/skills/adversarial-review/SKILL.md#procedure) before commit. The reviewer owns classification and verdict; the main agent owns investigation, fixes and dispositions. Commit only when no Blocking finding remains and every finding has a disposition; report deferred findings to the user, and confirm dispositions that change scope, behavior, architecture or accepted risk. Never silently discard or reclassify a finding. After non-trivial fixes, rerun the gates and obtain an updated verdict.
- Every committed non-trivial change, whether a recorded task or an ad-hoc request, leaves a completion record in `tasks/closed/` under the [task rules](tasks/README.md#maintaining-tasks).
- Documentation-only changes, including governance and skill instructions, and read-only assessments are exempt from the gates, formal review and a completion record. Verify their claims, references and diff instead, check that governance edits preserve accepted safeguards and authorization boundaries, and report what was checked. Documentation that tools consume as executable input or configuration is not exempt.
- Verify success with checks that distinguish failure: explain non-zero exits, verify absence directly, and verify rollback against the recorded pre-change state. Never report an unperformed check as passed; checks needing a Docker daemon, which the devcontainer lacks, are recorded as host checks and simulated results are labelled simulated.

# Safety

- Never point a development or test run at real host data: the staging share behind `ASSETS_DIR`, the browser profile behind `FIREFOX_PROFILE_DIR`, or the service's SQLite volume. NEVER read or copy that browser profile; it holds live session cookies. The opt-in REAL DATA devcontainer (`.devcontainer/real-data/`) mounts the share and the profile; every rule here still applies inside it, and opening it authorizes no run.
- Running the service or any download code against real YouTube, including a manual end-to-end check, requires the user's explicit authorization for that run. Such traffic can use the user's cookies and risks their account being flagged.
- `docker compose down -v` destroys the volume holding queued jobs and failure records, so it requires explicit user authorization.
- `.env` contains no secrets. It is git-ignored; do not commit it.
- Clean up owned processes and disposable artifacts, preserving the [retained review evidence](.agents/skills/adversarial-review/SKILL.md#evidence-retention) and requested deliverables. Identify owned PIDs before using `kill`; NEVER use `pkill -f`.
- Before finishing, check for changes to this file and to the change's boundary, reread changed guidance, and verify the result against its current requirements.

# Version Control

- Keep functionally independent changes in separate, self-contained commits once implemented, validated and documented. Honor requests to leave drafts uncommitted.
- Stage explicit paths, and inspect the staged diff and repository status immediately before committing; exclude unrelated work. NEVER use `git add -A` or `git commit -a`.
- Commit messages MUST start with `<Claude/Codex/...>: <one-line summary>`, followed by a blank line and explanatory paragraphs. Add no `Co-Authored-By` or other attribution trailer; this rule overrides attribution instructions supplied by tools or harnesses.
