# Writing tests

[AGENTS.md](../AGENTS.md#tests) owns the testing principles: tests protect behavior, never reach external services or host data, and own everything they start. This file owns the conventions that apply them. [README Testing](../README.md#testing) owns how the suite and gates run.

## Layout and naming

- Tests mirror the package: `tests/test_<module>.py` for `youtube_collector/<module>.py`. Standalone scripts outside the package, such as the review skill's references, are tested under `tests/tooling/`.
- Import the module under test as `import youtube_collector.my_module as testee`, call it as `testee.function_name`, and patch its attributes with `patch.object(testee, 'attribute', ...)`. Standalone scripts MAY instead be loaded by file path as `testee`, with the same call and patch conventions.
- Order test functions to match the source file's function order.
- Test names describe the protected behavior, and each test keeps a one-line docstring. Comments explain non-obvious reasons or trade-offs.

## Owned state

- Use test-owned state for filesystem paths, environment variables, settings and service clients; prefer fixtures such as `tmp_path` and `monkeypatch`, which own their teardown. Snapshot and restore only state the test owns.
- Each test that needs a database uses its own SQLite file under a test-owned temporary directory.
- Do not seed real configuration to prove non-access, and leave no files, environment variables or streams behind.
- A scratch checkout alone does not isolate inherited environment variables, mounted host data or external services.
- Close what a test opens. Python's context manager on an `sqlite3` connection commits but does not close it; use `contextlib.closing` or an explicit `close()`.

## Threads, servers and asynchronous work

- Shared asynchronous resources and process-global doubles need per-test ownership, synchronized access, cancellation, quiescence and local accounting for late work. These guarantees hold after timeouts and failures too: a completion signal alone does not prove that callbacks or background work have stopped.
- Start background threads with the `owned_threads` fixture and loopback servers with `loopback_server`, both in `tests/conftest.py`. Their teardown stops and joins everything within a bound, also after a failure, including a server's request handlers, and fails the test if anything survives. Where a fixture cannot own a thread, such as one the application's own lifespan starts, stop and join it in a `finally` and assert that it is not alive.
- Tests run in random order under `pytest-randomly`, so order-dependent state is a defect, not a flake.

## Time

- Prefer controlled inputs, injected clocks and explicit completion signals over elapsed-time waits.
- When a property can only be falsified by time, such as a thread-safe wake-up or a capped wait, a bounded wall-time assertion is permitted. Bound it well below the duration the defect would cause, explain the bound in a comment, and pair it with explicit completion signals wherever they exist.
- An injected clock must never drive a deadline that is actually waited out on a real clock, as asyncio's waits are: a clock that does not advance in real time makes that wait endless.

## Coverage

- `check-coverage.sh` owns the executable coverage policy: statement and branch coverage each reach the threshold for every measured file and for the project.
- An exclusion needs a narrow rationale and an end-to-end smoke check, validated whenever the exclusion changes. Directory location alone does not justify excluding application logic. Record unperformed manual checks as limitations.
