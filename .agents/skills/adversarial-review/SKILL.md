---
name: adversarial-review
description: Independently review non-trivial code, test, or configuration changes and return reviewer-owned findings without editing the repository. Use when AGENTS.md requires pre-commit review.
---

# Adversarial Review

[AGENTS.md](../../../AGENTS.md#validation-and-review) owns review requirements, gates and exemptions. This skill owns dispatch, investigation, triage and evidence handling. The reviewer investigates and classifies findings without editing the repository; the main agent owns implementation and dispositions. Documentation-only changes and read-only assessments use AGENTS' exemption.

## Review Scope

Review the complete target on its first assessment. One cohesive commit may cover tightly coupled tasks; independently committed changes each need review. Include every accepted boundary and integration path. Already reviewed tasks need another group review only for integration risks not previously covered; record that coverage decision. Follow-ups cover the delta, affected paths/invariants and unresolved findings. Substantial scope or design changes require full-target review again; integrated review reassesses earlier conclusions.

Verified cosmetic follow-ups may reuse gates and verdicts only when behavior, test guarantees, build/configuration output, security, concurrency, state/data flow and user-visible contracts cannot change. Check relevant dependencies, environment, modes and symlink targets; run affected lint/format and diff checks, record equivalence and updated fingerprints, and retain prior dispositions. Terminal blank-line cleanup is usually cosmetic; whitespace in strings/configuration or comments consumed as directives is not automatically so. If equivalence is uncertain, run full gates and review. Non-trivial fixes or rollback require full gates and an updated verdict.

## Reviewer Session

Keep one independent Codex reviewer per task or closely coupled group, retaining its history for follow-ups. Start it without the main agent's conversation. A replacement is allowed for unrelated work, substantial redesign or an unavailable reviewer; record why and transfer outstanding findings/dispositions. Compaction or a new user turn alone does not require replacement. Record agent/session identity, model and effort. A CLI reviewer may be unable to observe its own model or effort; the recorded dispatch command's model and effort arguments then serve as that record, and the reviewer should say so instead of asserting them.

- **Codex:** use `spawn_agent` with `fork_turns: "none"`, `model: "gpt-6.1-sol"`, and `reasoning_effort: "medium"`, unless the user selects otherwise. Continue the recorded reviewer with `followup_task`.
- **Claude Code:** verify `command -v codex` and `codex login status`, then use `codex exec --dangerously-bypass-approvals-and-sandbox --model gpt-6.1-sol -c model_reasoning_effort="medium" --json <review prompt> < /dev/null`. Record the returned session ID; continue with `codex exec resume --dangerously-bypass-approvals-and-sandbox --model gpt-6.1-sol -c model_reasoning_effort="medium" <SESSION_ID> <review prompt> < /dev/null`. Close stdin as shown: with an open non-terminal stdin, such as a background shell, codex-cli 0.160.1 waits for additional prompt input instead of starting. Use the recorded ID, not `--last`; add `--skip-git-repo-check` if required outside a repository. Missing setup/authentication is a blocker, not a reason to install a plugin.

[AGENTS](../../../AGENTS.md#scope-and-decisions) owns devcontainer and permission assumptions; enforced platform policy still applies. If the requested model or dispatch is unavailable, report the blocker and ask the user for direction. Do not substitute another model or dispatch path without that direction, and do not claim a review verdict. Track each invocation to completion and capture its completion status (exit status for CLI) and report before dispositioning findings.

## Procedure

### 1. Freeze, gate and snapshot — main agent

Freeze candidate edits and establish passing results for every required AGENTS gate on the exact target, using new runs or explicitly permitted evidence reuse. Resolve failures before dispatch. Record `git status`, content hashes/modes/symlink targets for dirty tracked and in-scope untracked files, out-of-scope untracked paths, and relevant owned processes. [snapshot.py](references/snapshot.py) prints deterministic JSON for everything except processes, including index object IDs and modes; invoke it with Python and pass root-relative in-scope untracked files as arguments. Store output outside the repository and compare only successful runs with `diff`; a failed run, including its refusal of submodules and of tracked paths below a working-tree symlink, is a change to investigate. Never inspect or hash ignored untracked content. Update the compact [working record](#evidence-retention). A temporary integrity snapshot can be discarded after a successful comparison unless needed for investigation or reuse.

### 2. Dispatch — main agent

Ask the [recorded reviewer](#reviewer-session) to read the current skill and perform step 3. Supply only:

- Repository path and exact target: dirty tree, commit or range; prior target and requested delta for follow-ups.
- In-scope paths, explicitly included untracked files, and unrelated dirty paths to ignore.
- Purpose in a few sentences.
- TODO location of the accepted boundaries, and the working-record location holding the evidence; a separate report destination if needed.

Add undocumented constraints, accepted findings not to re-raise, or other evidence only when needed. Do not inline the diff, conversation, repository architecture summaries, suspected defects or main-agent conclusions. Answer reviewer questions with minimum factual context and record material exchanges under Assumptions.

**Read-only repository.** Every dispatch keeps the repository read-only. The reviewer may write evidence outside it; tests, mutants, application execution and other commands requiring writable state use a disposable scratch copy. Source/diff reads and evidence output alone need no copy. Snapshot comparison detects accidents; it is not a security boundary.

**Scratch target.** Copy the tracked target state with its in-scope staged/unstaged changes, adding only explicitly named in-scope untracked files. Exclude ignored and unrelated content. Preserve executable bits and recreate repository-relative symlinks inside scratch; never dereference links into the original repository or external paths. Return `Review blocked` if a required target cannot be copied safely. Delete owned scratch at exit after retaining essential evidence.

**Execution isolation.** Scratch alone does not isolate settings, services or paths. Never copy or use `.env`, browser profiles, the staging share or the service's SQLite volume, and never contact YouTube. Read the [scratch-testing recipe](references/scratch-testing.md) before tests or mutants. Preserve the test bootstrap and owned resources, and verify exercised application imports come from scratch in every test process, including children, without changing the import lifecycle under test. Provenance/environment failures are invalid evidence, not mutation kills.

### 3. Investigate and report — reviewer

Read current AGENTS, the complete requested diff, in-scope untracked files and relevant source, tests, docs and configuration. Never read ignored untracked content. Actively try to falsify the change with boundary/failure cases, state transitions and tests that can pass for the wrong reason. Scrutinize SQLite transactions and revision ordering, the worker thread's handoff to the event loop, long-poll wake-ups, restart recovery, reconciliation deletes and the staging marker, atomic publication, resource and concurrency ownership, paths, and yt-dlp and caption failure handling where relevant. Assess correctness, preserved behavior, simplicity, design, documentation and testability; do not manufacture findings, treat taste as a defect or inflate severity.

Classify each evidenced finding by impact, likelihood, requirements and deferral risk:

- **Blocking:** must be resolved before commit.
- **Non-blocking:** a genuine issue that may reasonably be deferred.
- **Nits:** low-impact style/readability issues with an obvious local remedy.

Use safe, bounded verification. Rely on supplied passing gates for the frozen target; do not repeat the full suite without a specific concern. Run focused tests when needed. Apply AGENTS' mutation requirements, including removed/weakened coverage; [independently check supplied evidence](#evidence-retention) and fill required gaps in scratch or return `Review blocked`. Request additional mutations when protection is doubtful. A kill requires a relevant behavioral failure, not collection, syntax, import or environment failure. Record invariant, mutant, command/seed and outcome.

Preserve evidence, clean only owned scratch artifacts/processes, and return the [report](#report). Propose remedies if useful, but never implement repository edits.

### 4. Validate and disposition — main agent

Compare repository status, hashes, modes, symlink targets and out-of-scope untracked paths against step 1. An unexplained change blocks acceptance of the review. Then independently investigate findings without changing the reviewer's classification:

- **Blocking:** validate the claim and any remedy's premise/expected effect before implementing; fix validated findings. If not validated, return factual counterevidence to the reviewer. If a materially equivalent Blocking finding remains unvalidated across two consecutive assessments, stop and ask the user for direction.
- **Non-blocking:** investigate before implementing. Defer only with a reason and revisit trigger; material changes to scope, behavior, architecture or accepted risk require user confirmation.
- **Nits:** independently validate and fix when practical and in scope, otherwise record a justified deferral and revisit trigger.

Record each finding as **Validated**, **Could not validate**, or **Not independently validated**, with evidence when investigated and a disposition. Never silently discard or reclassify findings; only the reviewer updates its verdict. Report all Non-blocking findings and unfixed Nits to the user. Honor existing user dispositions; a deferral is not acceptance without change. Revalidate candidate changes under [Review Scope](#review-scope). Completion requires no unresolved Blocking review findings and a disposition for every finding; `Review blocked` is not a passing verdict.

## Report

Keep a reviewer-owned section in the working record, or a separate report if requested or clearer. Cite existing boundary/gate/mutation evidence rather than repeating it. Every finding gives classification, evidence, impact, path and a line where possible. Omit empty sections and dismissed hypotheses unless they explain a material decision. A clean review still gives verification and an explicit verdict. Follow-ups may describe only the delta, new verification, finding updates and revised verdict.

```markdown
## Adversarial Review Report

**Target:** <candidate and scope; prior target for follow-ups>
**Reviewer:** <agent/session, model/effort>
**Verification:** <paths/evidence checked, probes/results; reference gates>
**Assumptions and limitations:** <material qualifications>
**Cleanup:** <owned scratch/process cleanup, or none created>

### Findings

1. **Blocking | Non-blocking | Nit** — `path:line`: <defect, evidence, impact; optional remedy>

### Verdict

<No blocking findings | N blocking findings — fix and re-review | Review blocked — reason>
```

The main agent appends the integrity-comparison result and finding validations/dispositions. For a clean review, the comparison result suffices; do not duplicate the report or alter the reviewer-owned section.

## Evidence Retention

Create one compact [working record](references/review-record.md) per task or cohesive group before delegating evidence work. Keep it outside the repository and disposable scratch, at the user's requested compatible location or another writable location. Temporary storage is allowed with its lifetime limitation disclosed. Link reports and evidence from that record; avoid competing summaries. The template defines required fields and additional provenance for optional reuse.

Keep one authoritative accepted boundary, and retain candidate content for follow-up diffs: commit references for committed inputs, patches or equivalent snapshots for uncommitted inputs. Assign one primary mutation-evidence producer per cohesive candidate. The reviewer independently checks source/test identity, mutant and protected assertion, command/seed, actual-import provenance and behavioral outcome. Matching reliable evidence may be accepted without replay; record what was checked. Rerun affected cases when validity cannot be established, relevant inputs changed or a concrete concern needs reproduction; add a different probe when existing evidence cannot distinguish the suspected failure.

Select mutations by protected invariant, not task/function counts or a target number. For consolidation requiring evidence, retained tests must reject a representative regression for each distinct property; one mutant may protect multiple assertions. A changed mutant or selected test requires an affected-case rerun. Scratch-only probe corrections do not invalidate application gates or unrelated evidence when candidate, tests, dependencies, isolation and provenance still match. If uncertain, rerun the affected evidence. Substantive repository changes still require full gates and an updated verdict.

Evidence reuse is optional; rerunning is always an alternative. Follow the template's provenance checks for reused baselines, gates, mutants or verdicts. Identical tested content may support reuse across commit-metadata changes only for metadata-independent checks. Cosmetic reuse follows [Review Scope](#review-scope).

At closure, archive accepted boundaries, dispositions and essential evidence under [TODO maintenance](../../../TODO.md#maintaining-this-plan). That per-task archive rule applies when closing TODO tasks; other work still needs the retained record and handoff. Replace temporary links with durable artifacts before deleting essential evidence. A completion record supersedes the working summary and must not refer to it or to any other machine-local path. Preserve exact mutants, candidate/test identities, commands/seeds, import provenance and relevant failure excerpts; full passing logs and repeated tracebacks are optional. Keep raw output when needed to explain a finding. Remove disposable scratch, owned processes, edit scripts and unnecessary logs after transfer. User retention instructions take precedence; never retain secrets, ignored content, host data or copied external resources.
