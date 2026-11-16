# Tasks

This directory owns the state of tasks. This file owns how a task is recorded, bounded and closed. [STYLE.md](../STYLE.md#glossary) defines the terms.

- [TODO.md](TODO.md) holds the recommended order, the constraints that several tasks share, and the index of open tasks.
- [open/](open/) holds one file for each open task.
- [closed/](closed/) holds one completion record for each closed task and for each committed non-trivial ad-hoc change. The directory listing is the index of closed tasks.

## Two kinds of work

- A **recorded task** is opened in `open/` and closed into `closed/`.
- An **ad-hoc change** is requested directly and skips `open/`.

These rules apply to an ad-hoc change:

1. State its boundary to the user before implementation, as [AGENTS.md](../AGENTS.md#scope-and-decisions) requires for every change.
2. If the change is non-trivial, its commit adds a completion record at `closed/<ID>.md` with the next ID. The record uses the same skeleton and the same closing rules as a recorded task.
3. If the change is trivial or documentation-only, a descriptive commit message is enough.
4. If the change grows beyond one cohesive change, or will not finish in the session, record it as a task.

## Maintaining tasks

### Record a task

- Create `open/<ID>.md` at the moment you record the task. One file for each task keeps concurrent work away from the same lines.
- Add one line to TODO's index: the ID, the priority, the title and a link.
- Change that line only when the task is added, re-prioritized or closed.
- Give the task a stable ID. Never reuse an ID.
- State the severity or priority, the evidence status, the relevant paths, the expected outcome, the validation criteria and the dependencies.
- Keep a hypothesis distinguishable from a validated defect.
- Preserve the reviewer's classification, and record the user's dispositions.
- Keep the task compact until it is accepted for implementation.

### Bound a task

Before implementation, expand the task's file in place into a full boundary. Use the [skeleton](#open-task-file).

- The boundary adds four parts to the boundary that [AGENTS.md](../AGENTS.md#scope-and-decisions) requires: the implementation direction, the assumptions, the compatibility surface and the required mutation evidence.
- The compatibility surface names every HTTP contract, configuration setting and on-disk layout whose signature or meaning the change could affect. It states which of them are preserved.
- Python modules are internal, unless DESIGN declares a public API.
- The boundary must be in a committed file or in the candidate, because the working record is machine-local.
- The working record links to the boundary and does not copy it. Give the reviewer the boundary's path.
- If a group of tasks shares a constraint, state it once and link to it. Put it in TODO's shared constraints or in the first task's file.
- Each task keeps its own acceptance criteria.

### Record decisions

- Record each accepted decision in [DESIGN.md](../DESIGN.md), with its rationale and the tasks that it affects.
- Keep unresolved choices explicit.
- If a decision changes, update the affected task files and dependencies before the implementation commit.

### Choose the order

- Use the explicit dependencies and TODO's recommended order to choose the next task.
- A non-blocking issue does not block unrelated work. Do not require a whole phase to close first.

### Close a task

Do these steps in the closing commit:

1. Move `open/<ID>.md` to `closed/<ID>.md` with `git mv`. The accepted boundary then becomes the record's "Origin and boundary" without a copy.
2. Remove the task's line from TODO's index.
3. Replace the priority line under the title with a one-line `**Outcome:**`.
4. Append the remaining sections of the [closed record](#closed-record).
5. Fix the links that the move changed:
   - Inside the record, a sibling open task becomes `../open/<ID>.md`, and a closed task becomes `<ID>.md`.
   - Fix every link to the record from other files.
6. Update the documents that the task made stale:
   - DESIGN section 14 (architecture), for added or changed modules.
   - DESIGN section 7, for deployment.
   - README's status line and operating sections. Current operations belong in README.
   - TODO's recommended order.
   - Each open task that names the closed task.

The record preserves these items. Omit an item that does not apply.

- The task's origin, the accepted boundary and each superseding decision.
- The revalidation of the task against the code.
- The implementation, or another disposition.
- The validation commands and their results, with the revision and the environment.
- The mutation evidence that was required or performed.
- The review findings, with their validation and dispositions.
- The limitations and the cleanup.

These rules apply to the record:

- If a check or the review was not performed, state that and the reason. A review waiver is not a passed review.
- The record MUST be self-contained, because other machines see only committed files.
- The record MAY link other committed files.
- Keep essential evidence in the record or in a durable linked artifact, under the [evidence-retention rules](../.agents/skills/adversarial-review/SKILL.md#evidence-retention). Temporary paths alone are not an archive.

### If several tasks share evidence

- Each task keeps its own record and its own acceptance evidence.
- Link shared evidence by section, and do not repeat it. Identify the candidate that it applies to, and state any differences.
- Tasks that are committed separately keep their own validation, mutation outcomes and review verdict.
- A shared section does not make validation reusable. [Evidence reuse](../.agents/skills/adversarial-review/references/evidence-reuse.md) owns that decision.
- Use stable headings for linked sections. Short fields may stay inline.

### Defer a task, or accept it without change

- Record a deferral, or an acceptance without change, explicitly. Give the reason and a revisit trigger.
- A deferred task stays in `open/`.
- An obsolete task, or a task accepted without change, needs a closed record with its Outcome line.
- Do not drop inherited work silently.
- A documentation rewrite is not an implementation fix.

### After a restructure of the tasks

Check that the dependencies between the tasks are still consistent. The [documentation check](../README.md#testing) checks the links, the IDs and the TODO index.

## Open task file

```markdown
# <ID>: <title>

**Priority:** <severity or priority, and status such as deferred>.

<Compact entry: evidence, paths, outcome, validation, dependencies.>

## Origin and boundary

<Added when the task is accepted for implementation: where it comes from.>

- **Intent:** <what the change achieves, citing DESIGN sections and decisions>
- **Dependencies:** <tasks and decisions it relies on>
- **Implementation direction:** <modules, functions and the approach>
- **Decisions within DESIGN's latitude (assumptions):** <each assumption, its evidence and its effect>
- **Compatibility surface:** <every HTTP contract, setting or on-disk layout affected, and which are preserved>
- **Non-goals:** <what the task deliberately leaves out>
- **Validation:** <tests and checks that distinguish failure>
- **Mutation evidence:** <the invariants that need it>
- **Done:** <gates, review, closed record, host checks deferred to T10>
```

## Closed record

At closing, the file gains the `**Outcome:**` line and these sections after its boundary, as the records in [closed/](closed/) show:

- Implementation.
- Validation.
- Mutation evidence.
- Review.
- Host checks.
- Limitations.
