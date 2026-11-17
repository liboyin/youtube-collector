# Writing tests

[AGENTS.md](../AGENTS.md#tests) owns the testing rules. This file owns the conventions that apply them. [README Testing](../README.md#testing) owns how the suite and the gates run.

## Layout and naming

- Tests mirror the package: `tests/test_<module>.py` tests `youtube_collector/<module>.py`.
- Tests of standalone scripts outside the package live under `tests/tooling/`. The references of the review skill are such scripts.
- Import the module under test as `import youtube_collector.my_module as testee`. Call it as `testee.function_name`, and patch its attributes with `patch.object(testee, 'attribute', ...)`.
- A standalone script MAY be loaded by file path as `testee`, with the same conventions.
- Order the test functions to match the order of the functions in the source file.
- A test name describes the protected behavior. Each test keeps a one-line docstring.
- Comments explain non-obvious reasons or trade-offs.

Shared fixtures live in `conftest.py` files:

- `tests/conftest.py` installs the [network guard](#network) and owns test threads and loopback servers.
- `tests/conftest.py` also refuses `os._exit` in every test (`process_exits`). Take a deliberate attempt with `process_exits.take()`.
- `tests/tooling/conftest.py` builds the owned subprocess environment for the tests of shell tools.

## Owned state

- Use test-owned state for filesystem paths, environment variables, settings and service clients.
- Prefer fixtures that own their teardown, such as `tmp_path` and `monkeypatch`.
- Snapshot and restore only the state that the test owns.
- Do not seed real configuration to prove that the code does not access it.
- Leave no files, environment variables or streams behind.
- Close what a test opens. The context manager of an `sqlite3` connection commits but does not close. Use `contextlib.closing` or an explicit `close()`.

## Network

**The guard.** Every test runs under the network guard in `tests/network_guard.py`.

- The autouse fixture `network_refusals` in `tests/conftest.py` installs the guard for each test and removes it afterwards.
- The guard allows only loopback: `127.0.0.0/8`, `::1` and `localhost`.
- For any other destination, a connection, a datagram or a name lookup raises `ExternalNetworkError`, which is an `OSError`. The error names the destination.
- The code under test may swallow that error. Therefore each refused attempt also fails the test at teardown.

**Deliberate refusals.**

- A test that refuses on purpose calls `network_refusals.take()`. The guard's own tests do this.
- If an assertion can fail before that call, call it in a `finally`. The failed assertion then stays the test's only failure.
- For a test that must be refused, use a reserved destination: `192.0.2.1` (RFC 5737) or a name under `.invalid` (RFC 2606). These reach nothing even when the guard is broken, as under a mutant of the guard.

**The lifecycle service.**

- The lifecycle service installs the same guard for its whole process and logs each refusal.
- The `lifecycle` fixture fails the test if a service log holds a refusal.
- A test that expects refusals there sets `lifecycle.expect_refusals`.

**Limits of the guard.**

- The guard covers Python code that uses the `socket` module.
- It does not cover native code that opens its own sockets.
- It does not cover other child processes, such as ffmpeg, Deno or the shell scripts under test. Give these a fake or a loopback endpoint.

## Threads, servers and asynchronous work

- Start background threads with the `owned_threads` fixture, and loopback servers with `loopback_server`. Both are in `tests/conftest.py`.
- The teardown of these fixtures stops and joins everything within a bound, also after a failure. This includes a server's request handlers. The teardown fails the test if anything survives.
- If a fixture cannot own a thread, stop and join the thread in a `finally`, and assert that it is not alive. A thread that the application's own lifespan starts is an example.
- A test that needs a real service process uses the `lifecycle` fixture in `tests/test_main.py`. Its teardown kills and reaps every child.
- A shared asynchronous resource or a process-global double needs five things: ownership by one test, synchronized access, cancellation, quiescence, and local accounting for late work.
- These guarantees hold after timeouts and failures too. A completion signal alone does not prove that callbacks or background work have stopped.

## Time

- If only time can falsify a property, a bounded wall-time assertion is permitted. A thread-safe wake-up and a capped wait are examples.
- Bound the assertion well below the duration that the defect would cause, and explain the bound in a comment.
- Pair the assertion with explicit completion signals wherever they exist.
- An injected clock must never drive a deadline that the code waits out on a real clock, as asyncio's waits do. A clock that does not advance in real time makes that wait endless.

## Coverage

- An exclusion from coverage needs a narrow rationale and an end-to-end smoke check. Validate the check whenever the exclusion changes.
- The location of a directory alone does not justify the exclusion of application logic.
- Record each manual check that was not performed as a limitation.
