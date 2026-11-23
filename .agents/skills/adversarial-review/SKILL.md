---
name: adversarial-review
description: Independently review non-trivial code, test, or configuration changes and return reviewer-owned findings without editing the repository. Use when AGENTS.md requires pre-commit review.
---

# Adversarial Review

[AGENTS.md](../../../AGENTS.md#validation-and-review) owns when a review is required and which changes are exempt. This skill owns how a review runs.

## Review Scope

- The first assessment covers the complete candidate, with every accepted boundary and integration path.
- Tightly coupled tasks MAY share one commit and one review. Each independently committed change needs its own review.
- A follow-up covers the delta, the paths and invariants it affects, and the unresolved findings.
- If the scope or the design changes substantially, review the complete candidate again.
- An integrated review of a group reassesses the earlier conclusions.
- Tasks that were already reviewed need a further group review only for integration risks that no review covered. Record that decision.

If a follow-up change is cosmetic, or you want to reuse earlier gate results or evidence, read [evidence reuse](references/evidence-reuse.md) first.

## Reviewer Session

- Use one independent Codex reviewer for each task or closely coupled group. Keep its session for follow-ups.
- Start the reviewer without the main agent's conversation.
- Replace the reviewer only for unrelated work, a substantial redesign or an unavailable reviewer. Record the reason, and transfer the open findings and dispositions.
- Compaction or a new user turn alone is not a reason to replace the reviewer.

How to start the reviewer depends on the main agent:

- **Codex:** use `spawn_agent` with `fork_turns: "none"`, `model: "gpt-6.2-sol"` and `reasoning_effort: "high"`, unless the user selects otherwise. Continue the recorded reviewer with `followup_task`.
- **Claude Code:** run `../agent-workflow-common/tools/dispatch.py` from the repository root. It writes the prompt, starts or resumes the Codex CLI, and prints the session ID and the report. Its `--help` text lists the arguments.

If the requested model or dispatch path is unavailable:

1. Report the blocker to the user and ask for direction.
2. Do not use another model or dispatch path without that direction.
3. Do not claim a review verdict.

Wait for the reviewer's exit status and report before you decide on any finding.

## Procedure

### 1. Freeze, gate and snapshot (main agent)

1. Stop editing the candidate.
2. Obtain a passing result for every gate on the exact candidate. Resolve each failure before dispatch.
3. Take the integrity snapshot after the last gate run and immediately before dispatch. Run `../agent-workflow-common/tools/snapshot.py` from the repository root, and pass each in-scope untracked file as an argument.
4. Note the processes that you own, if any are relevant.
5. Update the [working record](#evidence-retention).

These rules apply to the snapshot:

- If you edit the candidate or run a gate on it after the snapshot, take a new snapshot.
- Write each snapshot outside the repository. A failed snapshot run blocks acceptance of the review.
- Never inspect or hash ignored untracked content.
- After a successful comparison, you MAY discard the snapshot, unless an investigation or evidence reuse needs it.

If you want to keep working while the candidate is under review, read [parallel candidates](references/parallel-candidates.md).

### 2. Dispatch (main agent)

Ask the [recorded reviewer](#reviewer-session) to read this skill and perform step 3. Supply only these items:

- The repository path and the exact candidate: a dirty tree, a commit or a range. For a follow-up, add the earlier candidate and the delta.
- The base revision. Read it from the candidate with `git rev-parse HEAD`.
- The in-scope paths, each included untracked file, and the unrelated dirty paths to ignore.
- The purpose, in a few sentences.
- The path to the accepted boundary. Under the [task rules](../../../tasks/README.md#two-kinds-of-work), this can be an open task or an ad-hoc completion record in the candidate.
- The location of the working record that holds the evidence, and a separate report destination if you need one.

These rules protect the reviewer's independence:

- Add a constraint, an accepted finding that the reviewer must not raise again, or other evidence only if the reviewer needs it.
- Do not send the diff, the conversation, a summary of the architecture, a suspected defect or your own conclusion.
- Answer a reviewer's question with the minimum of facts. Record each material exchange under Assumptions in the report.

**Read-only repository.** Every dispatch keeps the repository read-only.

- The reviewer MAY write evidence outside the repository.
- Reading the source or the diff needs no scratch copy.
- Anything that needs writable state runs in a scratch copy. This includes tests, mutants and the application.
- The snapshot comparison detects accidents. It is not a security boundary.

**Scratch runs.** Every scratch run MUST go through [mutate.py](references/mutate.py).

- Read the [scratch-testing recipe](references/scratch-testing.md) before the first run.
- If the tool cannot copy the candidate safely, return `Review blocked`.
- The tool copies every tracked file from the working tree, and only the named untracked ones. If tracked files outside the scope are dirty, freeze the candidate with [worktree_gates.py](references/worktree_gates.py) first. Then pass that worktree to `mutate.py` with `--repo`.
- A scratch copy does not isolate settings, services or host data. The [safety rules](../../../AGENTS.md#safety) apply to every scratch run.

### 3. Investigate and report (reviewer)

Read these before you judge the change:

- The current AGENTS.md.
- The complete diff and the in-scope untracked files. Never read ignored untracked content.
- The relevant source, tests, documentation and configuration.

Try to falsify the change:

- Probe boundary cases, failure cases and state transitions.
- Look for tests that can pass for the wrong reason.
- Where relevant, scrutinize these areas:
  - SQLite transactions and retention deletes.
  - The API's isolation from the worker thread.
  - Restart recovery and the staging marker.
  - Atomic publication.
  - Ownership of resources and of concurrent work.
  - Paths.
  - Failure handling for yt-dlp and for captions.
- Assess correctness, preserved behavior, simplicity, design, documentation and testability.
- Do not manufacture findings, treat taste as a defect, or inflate severity.

Classify each finding by its impact, its likelihood, the requirements and the risk of deferral:

- **Blocking:** the main agent must resolve it before the commit.
- **Non-blocking:** a genuine issue that the main agent may reasonably defer.
- **Nit:** a low-impact issue of style or readability with an obvious local remedy.

Verify safely and within bounds:

- Rely on the supplied passing gates for the frozen candidate. Do not repeat the full suite without a specific concern.
- If you need a focused test, run it with `mutate.py --tests`.
- Apply the [mutation requirements](../../../AGENTS.md#tests) of AGENTS.md, also for removed or weakened coverage.
- Check the supplied evidence independently, as [Evidence Retention](#evidence-retention) describes.
- If required evidence is missing, produce it in a scratch run or return `Review blocked`.
- If the protection of a property is doubtful, request more mutants.
- A kill needs a relevant behavioral failure. Collection, syntax, import, build, lint, environment and provenance failures are not kills.

Then finish:

1. Preserve the evidence.
2. Clean up only the scratch artifacts and processes that you own.
3. Return the [report](#report). You MAY propose a remedy.

### 4. Validate and decide (main agent)

1. Take a new snapshot and compare it with the snapshot from step 1. An unexplained change blocks acceptance of the review.
2. Investigate each finding yourself. Do not change the reviewer's classification.
3. Record each finding as **Validated**, **Could not validate** or **Not independently validated**. Add the evidence and the disposition.
4. Report every Non-blocking finding and every unfixed Nit to the user.

The class of a finding decides what you do with it:

- **Blocking:**
  - Validate the claim, and the premise and expected effect of any remedy, before you implement it. Fix each validated finding.
  - If you cannot validate the finding, return the factual counterevidence to the reviewer.
  - If an equivalent Blocking finding stays unvalidated in two consecutive assessments, stop and ask the user for direction.
- **Non-blocking:**
  - Investigate before you implement.
  - If you defer the finding, record the reason and a revisit trigger.
- **Nit:**
  - Validate and fix it if that is practical and in scope.
  - Otherwise, record a justified deferral and a revisit trigger.

These rules apply to every disposition:

- Honor the user's existing dispositions. A deferral is not acceptance without change.
- `Review blocked` is not a passing verdict.
- If you change the candidate, apply [Review Scope](#review-scope) to the change.

## Report

- The reviewer owns one section of the working record, or a separate report if that is requested or clearer.
- Cite the existing boundary, gate results and mutation evidence. Do not repeat them.
- Each finding gives its class, evidence, impact and path, and a line where possible.
- Omit empty sections. Omit a dismissed hypothesis unless it explains a material decision.
- A clean review still gives its verification and an explicit verdict.
- A follow-up MAY describe only the delta, the new verification, the updates to findings and the revised verdict.

```markdown
## Adversarial Review Report

**Target:** <the candidate and the scope. For a follow-up, the earlier target>
**Reviewer:** <the session, the model and the effort>
**Verification:** <the paths and evidence that you checked, and the probes with their results. Refer to the gates>
**Assumptions and limitations:** <material qualifications>
**Cleanup:** <the scratch copies and processes that you cleaned up, or "none created">

### Findings

1. **<Blocking, Non-blocking or Nit>** — `path:line`: <the defect, the evidence and the impact. A remedy is optional>

### Verdict

<One of: "No blocking findings". "N blocking findings. Fix and review again". "Review blocked", with the reason>
```

The main agent appends the result of the snapshot comparison, and the validation and disposition of each finding. The main agent does not alter the reviewer's section or repeat the report. For a clean review, the comparison result is enough.

## Evidence Retention

**The working record.**

- Create one [working record](references/review-record.md) for each task or cohesive group, before any evidence work starts.
- Keep it outside the repository and outside every scratch copy. Use the location that the user requests, or another writable location.
- Temporary storage is allowed. State its lifetime in the record.
- Link reports and evidence from the record. Do not keep competing summaries.
- Retain the candidate's content for follow-up diffs. Use commit references for committed inputs, and patches for uncommitted inputs.

**Mutation evidence.** [AGENTS.md](../../../AGENTS.md#tests) says when mutation evidence is required.

- The reviewer MAY also require it when ordinary tests give doubtful protection.
- One agent is the primary producer of mutation evidence for each candidate.
- The reviewer checks the producer's evidence: the identity of the source and the tests, the mutant, the command, the provenance and the behavioral failure.
- The reviewer MAY accept matching, reliable evidence without a rerun, and records what was checked.
- Rerun the affected cases if their validity cannot be established, a relevant input changed, or a concern needs reproduction.
- If the existing evidence cannot distinguish a suspected failure, add a different probe.
- A surviving mutant needs investigation. It is not a reason to remove coverage.
- If tests are consolidated, the retained tests must reject a representative regression of each distinct property.
- A mutant that bypasses validation or changes routing must still leave the tests in control of every resource. Inert configuration alone is not isolation.

**Closure.** The [task rules](../../../tasks/README.md#maintaining-tasks) own the completion record.

- Preserve the exact mutants, the identities of the candidate and the tests, the commands, the provenance and the relevant failure excerpts.
- Full passing logs and repeated tracebacks are optional. Keep raw output only if it explains a finding.
- Never retain secrets, ignored content, host data or copies of external resources.
