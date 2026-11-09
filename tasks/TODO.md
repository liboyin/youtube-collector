# TODO — Open work

Read [AGENTS.md](../AGENTS.md), [DESIGN.md](../DESIGN.md) and the relevant [README](../README.md) operating instructions before accepting an implementation task. This file holds the recommended order, the constraints several tasks share, and the index of open tasks. Each open task lives in its own file in [open/](open/); each closed task's record lives in [closed/](closed/); [README.md](README.md) owns how tasks are recorded, bounded and closed. DESIGN owns requirements, product contracts and accepted user decisions; AGENTS and the [adversarial review skill](../.agents/skills/adversarial-review/SKILL.md) own working policy and review.

Dependencies govern execution. A recorded task is not permission to combine unrelated changes or redesign the service. Accept a bounded assignment, re-read its code, and implement one independent change per commit. For a group of related tasks, use the [grouped task execution skill](../.agents/skills/grouped-task-execution/SKILL.md), which [AGENTS.md](../AGENTS.md) recommends.

## Recommended order

T25 needs the user's authorization for a run against real YouTube; T10 collects host checks as tasks land; T34 follows T25 and the user's orchestrator design; T14 waits for its revisit trigger.

T36 restores transaction exception safety, which T35 will rely on; the user deferred T35 (C51). T37 documents and validates C50's graceful shutdown with Docker termination fallback, and T38 must respect the worker lifetime when closing caption resources. T39 is deferred until a real run with cookies, authorized under T25, informs its backend choice (C52); coordinate its client ownership with T38 if they overlap. Hard-link validation stays in T25; no separate task is needed.

## Shared constraints

Checks that need a Docker daemon, which the dev container lacks, are host checks collected in [T10](open/T10.md); [README Testing](../README.md#testing) describes how to simulate `docker compose config` here.

Application modules live in `youtube_collector/`, one module per responsibility, with tests in `tests/` mirroring them (`tests/test_<module>.py`). Shared constraints for T15–T25: no test contacts YouTube or reads the Firefox profile, and every test keeps its files and SQLite databases in test-owned temporary directories ([AGENTS](../AGENTS.md#tests)); threads and waits are driven by injected clocks and explicit signals; components take explicit parameters, and only T24 reads the environment.

## Open tasks

- [T10](open/T10.md) — Low. Host checks that need Docker.
- [T14](open/T14.md) — Low, deferred. Let COVERAGE_THRESHOLD lower the coverage gate.
- [T25](open/T25.md) — Low. First end-to-end run against real YouTube.
- [T34](open/T34.md) — Medium, blocked on the user's orchestrator design. Integrate the orchestrator with the collector.
- [T35](open/T35.md) — Low, deferred; High review severity. Automatically recover work stranded by persistence failures.
- [T36](open/T36.md) — Medium. Restore transaction exception safety after COMMIT failure.
- [T37](open/T37.md) — Medium. Document and validate graceful shutdown with Docker termination fallback.
- [T38](open/T38.md) — Low. Give the caption HTTP session explicit lifecycle ownership.
- [T39](open/T39.md) — Medium, deferred until a run with cookies. Make configured Firefox authentication available to caption acquisition.
