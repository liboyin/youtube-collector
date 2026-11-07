# Scratch Testing Recipe

Use this recipe when the [review procedure](../SKILL.md#procedure) requires scratch execution. It supplies examples, not a substitute for AGENTS' test-ownership rules.

Create a scratch copy only when writable execution is needed. Within one agent's assessment of a fixed candidate, reuse one owned scratch tree for focused checks and mutants where practical, restoring the candidate before each mutation and using fresh test processes. Do not share a writable scratch tree between agents. A new assessment must use its current candidate; changing import-lifecycle requirements may justify a separate copy. Evidence inspection alone needs no scratch copy.

## Mutation selection and containment

Choose a focused test set that observes the protected behavior and owns every side effect reachable under the mutant. Inspect changed failure and routing paths before running it: a mutant that bypasses validation can reach resource creation that the passing test never exercised. Supply owned service doubles or appropriate scratch-only guards for those paths; an invalid hostname, synthetic credentials or temporary settings do not prevent network attempts. Preserve the import lifecycle under test when adding guards. For example, a routing probe can change the selected worker after role validation; a validation-bypass probe must also contain the resource creation that invalid input could now reach.

Establish a passing unmutated baseline for the selected tests with the same bootstrap, isolation and provenance checks. A mutation kill requires a behavioral failure for the named invariant. When probing premature acknowledgement or publication, make the mutant perform that side effect, rather than merely prepare an unexecuted operation, unless preparation itself is the protected invariant.

If a run escapes ownership or fails provenance, record it as invalid regardless of other assertion failures. Stop that probe, quiesce owned work, restore the candidate, and correct its containment or test selection before retrying; do not repeat the unsafe command unchanged. Retain a brief account of the escaped operation and any verified access or cleanup, without secrets. Follow the [evidence rules](../SKILL.md#evidence-retention) to rerun affected cases rather than automatically restart an otherwise valid batch.

## Fresh-process execution and provenance

Run fresh Python processes from the prepared scratch root using `python -m pytest`, with `PYTHONPATH` pointing to that root and `TMPDIR` plus pytest's `--basetemp` inside an owned scratch subdirectory. Preserve every copied `conftest.py`; today the only one is `tests/tooling/conftest.py`, which builds the owned subprocess environment for the shell-tool tests. Add scratch-only provenance checks in the process exercising the code without changing application import timing; a separate `find_spec` probe does not certify the pytest process. For focused tests whose exercised modules remain loaded, prefer checking `sys.modules` after execution. Add the following example only to the scratch copy of the conftest covering the selected tests, adapting the expected module names and preserving existing hooks and imports. It assumes a top-level `tests/conftest.py`, which application tests will need; `parents[1]` then resolves to the scratch root, so adjust it for a conftest deeper in the tree:

```python
from pathlib import Path
import sys

import pytest


def pytest_sessionfinish(session, exitstatus):
    """Verify exercised API provenance without preloading application modules."""
    scratch_root = Path(__file__).resolve().parents[1]
    names = ("youtube_collector.api.app",)
    for name in names:
        module = sys.modules.get(name)
        if module is None:
            raise pytest.UsageError(f"Expected exercised module is absent: {name}")
        location = Path(module.__file__).resolve()
        if not location.is_relative_to(scratch_root):
            raise pytest.UsageError(f"Application import escaped scratch: {name}: {location}")
    print(f"ACTUAL_IMPORT_PROVENANCE scratch={scratch_root} verified={','.join(names)}")
```

List only modules the selected tests are expected to exercise; modules intentionally kept absent MUST remain absent. If tests unload or replace modules, check provenance immediately after the relevant test-controlled import instead of relying on the session-end hook. For test-spawned child processes, add equivalent scratch-only location checks inside each child after its own configuration setup and controlled imports. A parent pytest check does not certify a child's imports. From the scratch root, a focused API run is:

```bash
mkdir -p owned-temp
PYTHONPATH="$PWD" PYTHONDONTWRITEBYTECODE=1 TMPDIR="$PWD/owned-temp" python -m pytest tests/api/test_app.py --no-cov --basetemp="$PWD/owned-temp/pytest"
```

A run failing the import check is invalid evidence, rather than a surviving or killed mutant. Save required evidence outside disposable scratch before cleanup. `PYTHONPATH` and owned temporary paths do not replace the test bootstrap's settings isolation or service doubles.

The focused command uses `--no-cov` because the repository's default pytest coverage threshold applies to the whole application. Preserve pytest-randomly's reported seed when reproducing a failure, and identify order-dependent results.

Retain the provenance line and the pytest outcome line for every run. For mutants, also retain the mutant diff and the excerpt of the relevant behavioral failure; full passing output, repeated tracebacks and per-test progress lines are not evidence.
