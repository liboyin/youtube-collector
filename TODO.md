# TODO — Future Work

Read [AGENTS.md](AGENTS.md), [DESIGN.md](DESIGN.md) and the relevant [README](README.md) operating instructions before accepting an implementation task. This file owns outstanding work, unresolved choices and task boundaries. DESIGN owns requirements, system design, product contracts and accepted user decisions; AGENTS and the [adversarial review skill](.agents/skills/adversarial-review/SKILL.md) own working policy and review.

Dependencies below govern execution. A recorded task is not permission to combine unrelated changes or redesign the service. Accept a bounded assignment, re-read its code, and implement one independent change per commit. Expand a compact backlog entry into a full boundary before implementing it.

## Maintaining this plan

This section owns how TODO is updated. General working principles belong in AGENTS; the review skill owns the review procedure. Keep this file focused on work that remains and dependencies on accepted decisions.

1. Give each task a stable ID, severity or priority, evidence status, relevant paths, expected outcome, validation criteria, and dependencies. Keep hypotheses distinguishable from validated defects; preserve reviewer classifications and record user dispositions. Do not recycle IDs.
2. Keep unassigned work compact. Before implementation, establish the assigned task's full boundary: intent, dependencies, implementation direction, affected compatibility surface, non-goals, validation, and done criteria. The compatibility surface names every HTTP contract, configuration setting or on-disk layout whose signature or semantics the change could affect, and states which are preserved. Python modules are internal unless DESIGN declares a public API. The boundary MAY live in this file or directly in the retained review record; keep one authoritative copy and supply its location to implementing and reviewing agents. When the record owns it, identify it by task ID and leave the compact TODO entry intact until completion rather than adding temporary boundary text here. State shared group constraints once; retain distinct acceptance criteria for each task.
3. Record accepted decisions with their rationale and affected tasks in [DESIGN.md](DESIGN.md). Keep unresolved choices explicit. When a decision changes, record its supersession and update affected boundaries and dependencies before the implementation commit.
4. Use explicit dependencies and readiness to guide execution. Do not build phase-closure fences or imply that every non-blocking issue must land before unrelated work can proceed.
5. When closing a task, create `completed_work/<ID>.md` in the same commit that removes its full active entry and execution boundary. Preserve its origin, accepted boundary and superseding decisions, revalidation, implementation or other disposition, validation commands and results with revision/environment provenance, required or performed mutation evidence, review findings and their validation/dispositions, limitations, and cleanup. State when checks or review were not performed and why; an explicit review waiver is not a passed review. Retain essential evidence in the record or durable linked artifacts under the [review skill's evidence-retention rules](.agents/skills/adversarial-review/SKILL.md#evidence-retention); temporary paths alone are not a completion archive. Omit inapplicable sections. Each task keeps its own record and acceptance evidence. Link shared evidence by section instead of repeating it, identifying the applicable candidate and any differences; separately committed tasks retain their own validation, mutation outcomes and review verdict. Sharing a record section does not establish eligibility to reuse validation. Leave one source line summarizing the outcome and linking the record under dispositions. Update references, dependencies, and affected counts or indexes; current operations belong in README. Use stable headings for linked sections; short fields may remain inline.
6. Record deferral or acceptance without change explicitly, with the reason and any revisit trigger. Closing an obsolete task or accepting it without change requires the completion record and disposition link above; deferred work remains active. Do not silently drop inherited work or treat a documentation rewrite as an implementation fix.
7. Keep evidence tied to its revision and provenance. Remove obsolete status inventories; historical review results do not certify an executor's current gates. Check local links and ID/dependency consistency after restructuring this file.

## Recommended execution workflow

Use the [grouped task execution skill](.agents/skills/grouped-task-execution/SKILL.md) for the user-recommended workflow; [AGENTS.md](AGENTS.md) owns its governance recommendation.

## Active backlog

The application work in [DESIGN.md](DESIGN.md) has not been broken into tasks yet; that planning step runs inside the devcontainer T1 created.

## Dispositions

- **T1** (devcontainer and project skeleton): implemented; gates and review passed; the image build is unverified because Docker was unavailable. [Record](completed_work/T1.md).
