# Tasks

This directory owns task state. [TODO.md](TODO.md) holds the recommended order, shared constraints and the index of open tasks; [open/](open/) holds one file per open task; [closed/](closed/) holds one completion record per closed task, and per committed non-trivial ad-hoc change. AGENTS owns general working principles and the [review skill](../.agents/skills/adversarial-review/SKILL.md) owns the review procedure; this file owns how tasks are recorded, bounded and closed.

Work arrives in two ways. A **recorded task** is opened in `open/` and closed into `closed/`. An **ad-hoc change**, requested directly rather than recorded beforehand, skips `open/`: it states its boundary to the user before implementation (AGENTS requires one for every change), and, if it is non-trivial, its commit adds a record straight to `closed/<ID>.md` with the next ID, using the same skeleton and the same closing rules (rule 5). Documentation-only and trivial ad-hoc changes need only a descriptive commit message. An ad-hoc request that grows beyond one cohesive change, or that will not finish in the session, becomes a recorded task.

## Maintaining tasks

1. **Recording.** Every open task has its own file, `open/<ID>.md`, from the moment it is recorded, and one line in TODO's index (ID, priority, title, link). Give each task a stable ID, severity or priority, evidence status, relevant paths, expected outcome, validation criteria and dependencies. Keep hypotheses distinguishable from validated defects; preserve reviewer classifications and record user dispositions. Do not recycle IDs. One file per task keeps concurrent work from editing the same lines; the TODO index line changes only when a task is added, re-prioritized or closed.
2. **Bounding.** Keep unassigned work compact. Before implementation, expand the task's file in place into a full boundary, using the skeleton below. It extends the boundary [AGENTS](../AGENTS.md#scope-and-decisions) requires of every change (intent, dependencies, non-goals, validation, done criteria) with the implementation direction, assumptions, the affected compatibility surface and the required mutation evidence. The compatibility surface names every HTTP contract, configuration setting or on-disk layout whose signature or semantics the change could affect, and states which are preserved. Python modules are internal unless DESIGN declares a public API. The boundary is committed, or ships in the candidate, because the review working record is machine-local; the working record links to it rather than copying it, and the reviewer is given its path. State constraints shared by a group once, in TODO's shared constraints or the first task's file, and link them; retain distinct acceptance criteria for each task.
3. **Decisions.** Record accepted decisions with their rationale and affected tasks in [DESIGN.md](../DESIGN.md). Keep unresolved choices explicit. When a decision changes, record its supersession and update affected task files and dependencies before the implementation commit.
4. **Readiness.** Use explicit dependencies and readiness, and TODO's recommended order, to guide execution. Do not build phase-closure fences or imply that every non-blocking issue must land before unrelated work can proceed.
5. **Closing.** Move `open/<ID>.md` to `closed/<ID>.md` with `git mv` in the closing commit, so the accepted boundary becomes the record's "Origin and boundary" without copying, remove its line from TODO's index, put a one-line `**Outcome:**` under the record's title, and append the remaining sections. Preserve the task's origin, accepted boundary and superseding decisions, revalidation, implementation or other disposition, validation commands and results with revision/environment provenance, required or performed mutation evidence, review findings and their validation/dispositions, limitations, and cleanup. State when checks or review were not performed and why; an explicit review waiver is not a passed review. Retain essential evidence in the record or durable linked artifacts under the [review skill's evidence-retention rules](../.agents/skills/adversarial-review/SKILL.md#evidence-retention); temporary paths alone are not a completion archive. The committed archive is the only evidence other machines see, so it MUST be self-contained: a record MAY link other committed files, but never machine-local paths such as session scratch or a working record. Omit inapplicable sections. Each task keeps its own record and acceptance evidence. Link shared evidence by section instead of repeating it, identifying the applicable candidate and any differences; separately committed tasks retain their own validation, mutation outcomes and review verdict. Sharing a record section does not establish eligibility to reuse validation. Use stable headings for linked sections; short fields may remain inline. In the same commit, update the documents the task made stale: DESIGN section 14 (architecture) for added or changed modules, DESIGN section 7 for deployment, README's status line and operating sections, TODO's recommended order, and any open task that named the closed one; current operations belong in README. Moving the file changes what its relative links resolve to: in the same commit, fix the links inside the record (a sibling open task becomes `../open/<ID>.md`, and a closed one `<ID>.md`) and every link to it from other files, then check that they all resolve.
6. **Deferral and acceptance without change.** Record deferral or acceptance without change explicitly, with the reason and any revisit trigger. Closing an obsolete task or accepting it without change requires a closed record with its Outcome line, as above; deferred work stays in `open/`. Do not silently drop inherited work or treat a documentation rewrite as an implementation fix.
7. **Evidence and consistency.** Keep evidence tied to its revision and provenance; historical review results do not certify an executor's current gates. After restructuring, check local links, the TODO index against `open/`, and ID and dependency consistency.

## Open task file

A newly recorded task starts compact; it is expanded in place before implementation. Keep one source line per prose paragraph.

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

At closing, the file gains the `**Outcome:**` line under its title (replacing the priority line) and the sections Implementation, Validation, Mutation evidence, Review, Host checks and Limitations after its boundary, as the records in [closed/](closed/) show. The directory listing is the index of closed tasks; there is no separate dispositions list.
