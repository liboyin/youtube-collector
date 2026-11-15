# TODO — Open work

This file holds the recommended order, the constraints that several tasks share, and the index of open tasks. [README.md](README.md) owns how tasks are recorded, bounded and closed.

- Read [AGENTS.md](../AGENTS.md), [DESIGN.md](../DESIGN.md) and the relevant operating instructions in the [project README](../README.md) before you accept an implementation task.
- A recorded task is not permission to combine unrelated changes or to redesign the service.
- For a group of related tasks, use the [grouped task execution skill](../.agents/skills/grouped-task-execution/SKILL.md).

## Recommended order

The decisions of the 2026-11-14 review (DESIGN C53 to C60) produced the mechanism of T35 and the tasks T40 to T52. This is the suggested sequence. Each group is small enough for the grouped-task skill.

1. T35: crash and restart. Land it before the next group, which rewrites the same worker code.
2. T40, then T41 and T42: local scratch, publication order and health. T41 and T42 depend on T40.
3. T43, then T44: the cookie setting and the report of stale cookies. They add `/health` fields, as T42 does, so do not run them in parallel with T42.
4. T51, then T46 and T47: the caption interface, then the contract tests and end-to-end tests that are written against the result.
5. T49, then the remaining runs of T25 with it. T48 goes alongside the host checks of T10. T52 can land at any time after groups 2 and 4.

These tasks are outside the sequence:

- T25 needs the user's authorization for each run against real YouTube. Hard-link validation stays in T25.
- T10 collects host checks as tasks land.
- T34 follows T25 and the user's orchestrator design.
- T14 waits for its revisit trigger.
- T39 stays deferred until a real run with cookies, authorized under T25, informs its choice of backend (C52). T43, T44 and T51 make that run and that change easier.
- T55 and T56 change only the review tooling and the task checks. They can land at any time.

## Shared constraints

A check that needs a Docker daemon is a host check, because the dev container has no daemon. Collect host checks in [T10](open/T10.md). [README Testing](../README.md#testing) describes how to simulate `docker compose config` in the dev container.

## Open tasks

- [T10](open/T10.md) — Low. Host checks that need Docker.
- [T14](open/T14.md) — Low, deferred. Let COVERAGE_THRESHOLD lower the coverage gate.
- [T25](open/T25.md) — Low. First end-to-end run against real YouTube.
- [T34](open/T34.md) — Medium, blocked on the user's orchestrator design. Integrate the orchestrator with the collector.
- [T35](open/T35.md) — High. Exit the process when the worker cannot continue, so Docker restarts it (C53, C59).
- [T39](open/T39.md) — Medium, deferred until a run with cookies. Make configured Firefox authentication available to caption acquisition.
- [T40](open/T40.md) — High. Download and merge media in a local scratch directory, then copy it to the share (C54).
- [T41](open/T41.md) — Medium. Publish the SRT before the MP4 (C57).
- [T42](open/T42.md) — High. Judge staging staleness only when the worker can touch the share (C55).
- [T43](open/T43.md) — Medium. Name the Firefox profile with an explicit setting and report missing cookies (C56).
- [T44](open/T44.md) — Medium. Report stale or rotated cookies (C56).
- [T46](open/T46.md) — Medium. Adapter contract tests against the real libraries (C58).
- [T47](open/T47.md) — Medium. Offline end-to-end tests of both workflows against a real service process (C58).
- [T48](open/T48.md) — Low. Script the image checks that need no YouTube (C58).
- [T49](open/T49.md) — Medium. A scripted, opt-in live canary against real YouTube (C58).
- [T51](open/T51.md) — Low. Give the caption adapter one method and drop the track protocols.
- [T52](open/T52.md) — Low. Small cleanups from the 2026-11-14 review.
- [T55](open/T55.md) — Low. Check the task files for consistency with a test.
- [T56](open/T56.md) — Low. Stop a nested pytest's output from making a mutation run INVALID.
