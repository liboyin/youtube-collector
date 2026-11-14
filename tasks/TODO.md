# TODO — Open work

Read [AGENTS.md](../AGENTS.md), [DESIGN.md](../DESIGN.md) and the relevant [README](../README.md) operating instructions before accepting an implementation task. This file holds the recommended order, the constraints several tasks share, and the index of open tasks. Each open task lives in its own file in [open/](open/); each closed task's record lives in [closed/](closed/); [README.md](README.md) owns how tasks are recorded, bounded and closed. DESIGN owns requirements, product contracts and accepted user decisions; AGENTS and the [adversarial review skill](../.agents/skills/adversarial-review/SKILL.md) own working policy and review.

Dependencies govern execution. A recorded task is not permission to combine unrelated changes or redesign the service. Accept a bounded assignment, re-read its code, and implement one independent change per commit. For a group of related tasks, use the [grouped task execution skill](../.agents/skills/grouped-task-execution/SKILL.md), which [AGENTS.md](../AGENTS.md) recommends.

## Recommended order

The 2026-11-14 review's decisions (DESIGN C53–C60) produced T35's mechanism and T40–T52. Suggested sequence, each group small enough for the grouped-task skill:

1. T45 and T50: independent and small; T45 is the one validated defect.
2. T35: crash and restart. Land it before the next group, which rewrites the same worker code.
3. T40, then T41 and T42: local scratch, publication order and health. T41 and T42 depend on T40.
4. T43, then T44: the cookie setting and stale-cookie reporting. They add `/health` fields, as T42 does, so do not run them in parallel with it.
5. T51, then T46 and T47: the caption interface, then the contract and end-to-end tests written against the result.
6. T49, then T25's remaining runs with it; T48 alongside T10's host checks. T52 any time after groups 3 and 5.

T25 needs the user's authorization for each run against real YouTube; T10 collects host checks as tasks land; T34 follows T25 and the user's orchestrator design; T14 waits for its revisit trigger. T39 stays deferred until a real run with cookies, authorized under T25, informs its backend choice (C52); T43, T44 and T51 make that run and that change easier. Hard-link validation stays in T25.

## Shared constraints

Checks that need a Docker daemon, which the dev container lacks, are host checks collected in [T10](open/T10.md); [README Testing](../README.md#testing) describes how to simulate `docker compose config` here.

Application modules live in `youtube_collector/`, one module per responsibility, with tests in `tests/` mirroring them (`tests/test_<module>.py`). Shared constraints for T15–T25: no test contacts YouTube or reads the Firefox profile, and every test keeps its files and SQLite databases in test-owned temporary directories ([AGENTS](../AGENTS.md#tests)); threads and waits are driven by injected clocks and explicit signals; components take explicit parameters, and only T24 reads the environment.

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
- [T45](open/T45.md) — Medium. Categorize real yt-dlp errors correctly.
- [T46](open/T46.md) — Medium. Adapter contract tests against the real libraries (C58).
- [T47](open/T47.md) — Medium. Offline end-to-end tests of both workflows against a real service process (C58).
- [T48](open/T48.md) — Low. Script the image checks that need no YouTube (C58).
- [T49](open/T49.md) — Medium. A scripted, opt-in live canary against real YouTube (C58).
- [T50](open/T50.md) — Medium. Fail any test that opens a connection beyond loopback (C58).
- [T51](open/T51.md) — Low. Give the caption adapter one method and drop the track protocols.
- [T52](open/T52.md) — Low. Small cleanups from the 2026-11-14 review.
