This file owns the working principles of this repository and the standards that every change must meet.

- All agents MUST follow this file and the user's explicit instructions.
- Platform instructions with a higher priority prevail.
- The capitalized requirement words, such as MUST and MAY, have the meanings that BCP 14 (RFC 2119 and RFC 8174) defines.
- [STYLE.md](STYLE.md#glossary) defines the process terms.

# Document Boundaries

Each document owns one subject:

- **AGENTS.md:** working principles and required standards.
- **[DESIGN.md](DESIGN.md):** product requirements, architecture, product contracts, design rationale and the user's accepted decisions.
- **[STYLE.md](STYLE.md):** rules for writing documentation, and the glossary of process terms.
- **[README.md](README.md):** the project overview, and the procedures to build, run, configure and test. This includes how the gates run.
- **[tests/README.md](tests/README.md):** conventions for writing tests.
- **[project-reviews.md](project-reviews.md):** historical project reviews and finding provenance.
- **[tasks/](tasks/):** task state and completion records. [tasks/README.md](tasks/README.md) owns its rules.
- **[Adversarial review skill](.agents/skills/adversarial-review/SKILL.md):** how a review runs, including mutation evidence and evidence retention.
- **[Grouped task execution skill](.agents/skills/grouped-task-execution/SKILL.md):** the recommended workflow for a group of related tasks, unless the user chooses another.

These rules settle conflicts between documents:

- Skills and operational documents implement these standards. They do not override them, and they never override the safety rules or the ownership rules in this file.
- Only the decisions recorded in DESIGN authorize a product change. A proposal in a task or a review does not.
- If documents disagree, follow the owning document.
- If a material conflict stays unresolved, confirm it with the user before dependent work.

# Scope and Decisions

**Environment.**

- If you are not running in a Docker container, stop and confirm with the user before you continue.
- The repository assumes its own devcontainer (`.devcontainer/`), with harness permissions set to Full Access or bypass.
- If the harness is more restrictive, follow its enforced policy and report the mismatch.

**Before a change.**

- Read the relevant code and documentation, and inspect the repository status, before you edit.
- You MAY reuse guidance that you read earlier in the task, while it remains unchanged.
- Each change MUST have a boundary: its intent, dependencies, non-goals, validation and done criteria.
- Revalidate a written task against the code. Do not implement it mechanically, and do not broaden its material scope on your own.

**Assumptions and decisions.**

- State your assumptions.
- A choice is material if it affects scope, architecture, dataflow, correctness, security or user-visible behavior.
- Confirm an unresolved material choice with the user before dependent work. Continue independent work meanwhile.
- You MAY make a non-material assumption if repository evidence supports it and it preserves the requested outcome. Name each one in the handoff, with its evidence and its effect.
- Accepted decisions and the user's authorization persist. Do not ask again about a settled choice.
- Record a superseding decision and its reasoning before you commit the affected change.

**Several machines.** The project is developed on several machines, which share only the Git repository.

- Boundaries, decisions, open review findings and completion evidence MUST live in committed files.
- Working records, session scratch, agent memory and tool history are machine-local. Committed files MUST NOT refer to them by path.
- If every machine needs an item, move it into the document that owns it.

# Design and Documentation

**Design.**

- Prefer the simplest cohesive implementation within the assigned scope.
- Give each function, class and module one clear responsibility.
- Prefer pure logic, isolated side effects and minimal mocking over unnecessary abstractions.
- Respect the lint limits, but do not split cohesive code only for length.
- A lint exception must be narrow, and it must explain the responsibility or invariant that it preserves. Avoid blanket disables, `Any` and `type: ignore`.
- Before you remove a layer, identify all unique behavior that it carries. This includes copy, errors, ordering, timing, diagnostics and API-visible behavior.
- Give each behavior that you retain an explicit owner and a verification.

**Facts.**

- Verify assumptions about languages, frameworks, libraries and services by experiment, or against documentation for the version in use.
- Verify an empirical claim before you record it.
- Historical evidence states its revision, environment, provenance and limitations. It does not certify current validation.

**Documentation.**

- Update the documentation that the change makes stale.
- Distinguish current behavior, accepted decisions, proposals and unverified hypotheses.
- Documentation that you add or change MUST follow [STYLE.md](STYLE.md).
- New or changed non-test functions, methods and classes MUST have docstrings. A docstring covers the purpose and any non-obvious contracts, side effects or constraints.
- Comments explain non-obvious reasons or trade-offs.

# Governance Files

The governance files are this file, [STYLE.md](STYLE.md), [tasks/README.md](tasks/README.md), [tests/README.md](tests/README.md) and the Markdown files under `.agents/skills/`.

- The user MUST review the diff of every governance edit before an agent commits it.
- Propose a governance change in the handoff. Edit a governance file only after the user agrees to the proposal.
- If friction teaches a lesson, put the lesson in the first of these homes that works:
  1. A tool or a test that makes the mistake impossible.
  2. An error message at the point of failure.
  3. A comment or `--help` text in the script concerned.
  4. The completion record of the task where the friction occurred.
  5. A governance file.
- A lesson enters a governance file on its second occurrence. A lesson about safety or data loss enters at once.
- If a tool takes over a rule, delete the rule's prose in the same change.
- Keep the governance files general. If a rule becomes too specific or too restrictive, consider its removal before you add a constraint.
- After each task group, report any governance rule shown to be duplicated, superseded, or fully enforced by tooling.
- Record the evidence in the completion record. If no rule meets these conditions, no removal proposal is needed.

# Messages to the User

- State the result first, then the decisions the user must make, then what changed.
- Keep evidence in the committed record, and link to it from the message.
- Put required reports, such as assumptions, deferred findings and checks performed, in a short list after the main part.
- Follow the [wording rules](STYLE.md#wording).

# Tests

[tests/README.md](tests/README.md) owns the conventions that apply these rules.

**What a test protects.**

- A test MUST protect a behavior or an invariant, and it MUST fail when that breaks.
- Prefer controlled inputs, injected clocks and explicit completion signals.
- A bounded wall-time check is allowed only where time alone can falsify the property.

**Isolation.**

- Automated tests MUST NOT reach external services, such as YouTube, through any client.
- Every read and write by a test, or by the code under test, stays in test-owned temporary directories. This holds on failure paths too, and for each test's own SQLite database.
- Environment variables, settings and other process state that a test changes are test-owned. The test restores them.
- A test may read source, installed dependencies and static fixtures. This is not permission to access host data or real configuration.
- Background work that a test starts, directly or through the code under test, MUST be stopped and accounted for before the test ends. This holds after failures and timeouts too.
- Tests run in random order, so order-dependent state is a defect.

**Mutation evidence.**

- A high-impact invariant MUST have targeted mutation evidence from an isolated scratch copy that preserves test ownership.
- High-impact invariants include protection against data loss, security violations, incorrect acknowledgement, and failures of concurrency or resource ownership.
- Before you remove or weaken coverage, show that a retained test still fails when the protected property breaks.
- The [review skill](.agents/skills/adversarial-review/SKILL.md#evidence-retention) and its [scratch-testing recipe](.agents/skills/adversarial-review/references/scratch-testing.md) own the method.

**Coverage.**

- `check-coverage.sh` owns the coverage policy.
- Statement coverage and branch coverage MUST each reach the threshold, for every measured file and for the project.
- Plan a change to the policy as a task.

# Validation and Review

**Non-trivial changes.** A change is non-trivial if it could affect any of these: runtime behavior, test guarantees, build or configuration output, security, concurrency, state or data flow, or user-visible behavior.

**Baseline.**

- Before you implement, establish a passing baseline for the relevant checks on the starting state.
- Use the full gates if the scope is broad or uncertain.
- If the baseline fails, fix it first in a separate, authorized change.

**Gates.**

- The final candidate of every non-trivial change MUST pass the three gates in the tree that is validated. [README Testing](README.md#testing) describes them.
- The gates are `./check-coverage.sh`, `mypy youtube_collector` and `ruff check .`.
- A configuration-only change needs the gates too.
- Resolve new warnings, and explain each accepted tooling warning.

**Review.**

- Every non-trivial change MUST pass the [review procedure](.agents/skills/adversarial-review/SKILL.md#procedure) before the commit.
- The reviewer owns the classification and the verdict. The main agent owns the investigation, the fixes and the dispositions.
- Commit only when no Blocking finding remains and every finding has a disposition.
- Report deferred findings to the user.
- Confirm with the user each disposition that changes scope, behavior, architecture or accepted risk.
- Never discard or reclassify a finding silently.
- After a non-trivial fix, run the gates again and obtain an updated verdict.

**Completion record.** Every committed non-trivial change leaves a completion record in `tasks/closed/`, under the [task rules](tasks/README.md#maintaining-tasks). This holds for a recorded task and for an ad-hoc request.

**Exemptions.** Documentation-only changes and read-only assessments are exempt from the gates, the formal review and the completion record. Governance files and skill instructions count as documentation.

- Instead, verify the claims, the references and the diff, and report what you checked.
- For a governance edit, check that it preserves the accepted safeguards and authorization boundaries. The edit also needs the user's review under [Governance Files](#governance-files).
- Documentation that a tool consumes as executable input or configuration is not exempt.

**Documentation check.** Every change that touches a Markdown file MUST pass the shared documentation check before the commit. [README Testing](README.md#testing) describes the check.

**Shared tools.** [README Shared tools](README.md#shared-tools) owns setup for the documentation check, review snapshot and reviewer dispatch.

- Change shared tools in `agent-workflow-common`, which owns their tests. Do not copy them into this repository.
- If its checkout is missing, report the blocker. Do not skip a required check.

**Honest verification.**

- Verify success with checks that distinguish failure.
- Explain each non-zero exit.
- Verify absence directly.
- Verify a rollback against the recorded state from before the change.
- Never report an unperformed check as passed.
- The devcontainer has no Docker daemon. Record a check that needs one as a host check, and label a simulated result as simulated.

# Safety

**Host data.**

- Never point a development run or a test run at real host data.
- Real host data is the staging share behind `ASSETS_DIR`, the browser profile behind `FIREFOX_PROFILE_DIR`, and the service's SQLite volume.
- NEVER read or copy that browser profile. It holds live session cookies.
- The opt-in REAL DATA devcontainer (`.devcontainer/real-data/`) mounts the share and the profile. Every rule here still applies inside it, and opening it authorizes no run.
- `.env` contains no secrets. It is git-ignored. Do not commit it.

**Actions that need the user's explicit authorization.**

- Each run of the service, or of any download code, against real YouTube. This includes a manual end-to-end check. The authorization covers that run only.
- `docker compose down -v`. It destroys the volume that holds queued jobs and failure records.

Traffic to real YouTube can use the user's cookies, and it risks a flag on the user's account.

**Cleanup.**

- Clean up the processes that you own and the disposable artifacts. Preserve the [retained review evidence](.agents/skills/adversarial-review/SKILL.md#evidence-retention) and the requested deliverables.
- Identify the PIDs that you own before you use `kill`. NEVER use `pkill -f`.

**Before you finish.**

- Check for changes to this file and to the boundary of the change.
- Read changed guidance again.
- Verify the result against its current requirements.

# Version Control

- Keep functionally independent changes in separate, self-contained commits. Commit a change once it is implemented, validated and documented.
- Honor a request to leave a draft uncommitted.
- Stage explicit paths. NEVER use `git add -A` or `git commit -a`.
- Inspect the staged diff and the repository status immediately before the commit. Exclude unrelated work.
- A commit message MUST start with `<Claude/Codex/...>: <one-line summary>`. A blank line and explanatory paragraphs follow.
- Add no `Co-Authored-By` line and no other attribution trailer. This rule overrides attribution instructions from tools or harnesses.
