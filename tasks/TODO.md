# TODO — Open work

Read [AGENTS.md](../AGENTS.md), [DESIGN.md](../DESIGN.md) and the relevant [README](../README.md) operating instructions before accepting an implementation task. This file holds the recommended order, the constraints several tasks share, and the index of open tasks. Each open task lives in its own file in [open/](open/); each closed task's record lives in [closed/](closed/); [README.md](README.md) owns how tasks are recorded, bounded and closed. DESIGN owns requirements, product contracts and accepted user decisions; AGENTS and the [adversarial review skill](../.agents/skills/adversarial-review/SKILL.md) own working policy and review.

Dependencies govern execution. A recorded task is not permission to combine unrelated changes or redesign the service. Accept a bounded assignment, re-read its code, and implement one independent change per commit. For a group of related tasks, use the [grouped task execution skill](../.agents/skills/grouped-task-execution/SKILL.md), which [AGENTS.md](../AGENTS.md) recommends.

## Recommended order

T32 (file names) and then T33 (job lifecycle) come first, because they change the file layout and API that T25 would exercise; T26 then T27, and T28, are independent tooling work; T31 (typed response models) follows T33; T25 needs the user's authorization for a run against real YouTube; T10 collects host checks as tasks land; T14 waits for its revisit trigger.

## Shared constraints

Checks that need a Docker daemon, which the dev container lacks, are host checks collected in [T10](open/T10.md); [README Testing](../README.md#testing) describes how to simulate `docker compose config` here.

Application modules live in `youtube_collector/`, one module per responsibility, with tests in `tests/` mirroring them (`tests/test_<module>.py`). Shared constraints for T15–T25: no test contacts YouTube or reads the Firefox profile, and every test keeps its files and SQLite databases in test-owned temporary directories ([AGENTS](../AGENTS.md#tests)); threads and waits are driven by injected clocks and explicit signals; components take explicit parameters, and only T24 reads the environment.

## Open tasks

- [T10](open/T10.md) — Low. Host checks that need Docker.
- [T14](open/T14.md) — Low, deferred. Let COVERAGE_THRESHOLD lower the coverage gate.
- [T25](open/T25.md) — Low. First end-to-end run against real YouTube.
- [T26](open/T26.md) — Medium. Make the coverage gate refuse a foreign package.
- [T27](open/T27.md) — Medium. Commit the review tooling.
- [T28](open/T28.md) — Medium. Shared test fixtures for owned threads and loopback servers.
- [T31](open/T31.md) — Low. Typed response models.
- [T32](open/T32.md) — High. Name files with the ID first.
- [T33](open/T33.md) — High. Simplify the job lifecycle.
