# youtube-collector

Download YouTube videos, playlists and captions into a staging area, as a LAN-only HTTP service.

- [DESIGN.md](DESIGN.md) owns the requirements and the design.
- [tasks/TODO.md](tasks/TODO.md) owns the outstanding work.
- [AGENTS.md](AGENTS.md) owns the working standards.

**Status:**

- The service is implemented and tested against fakes. The HTTP API and the YouTube worker thread run in one process ([DESIGN section 14](DESIGN.md#14-architecture)).
- Against real YouTube, it has run only from the dev container and without cookies. Those runs covered media jobs and caption jobs with manual tracks.
- The real staging share is still unverified (task [T25](tasks/open/T25.md)).
- Task [T10](tasks/open/T10.md) lists the container-level checks.

## Security / trust model

The service is unauthenticated and intended for a trusted LAN only. Do not expose it to the public internet. [DESIGN section 7](DESIGN.md#7-deployment-and-operation) describes the deployment.

## Dev Container (recommended)

Open the repository in VS Code and reopen it in the Dev Container (`.devcontainer/`).

**The image.**

- The container's `app` service builds the Dockerfile's `dev` target (`.devcontainer/docker-compose.devcontainer.yaml`).
- That target installs the locked runtime dependencies, the `dev` dependencies and the editable project into the venv at `/opt/venv`. The venv is first on `PATH`.
- If `PYPI_PROXY` is set, every PyPI package is fetched through it.
- The image is Ubuntu 26.04 with its distro Python 3.14. It also contains ffmpeg, Deno (yt-dlp's JavaScript runtime), uv, git and curl.
- The service's `runtime` target keeps only ffmpeg, Deno, Python and the venv. There, the project is installed as a regular package.
- The build fails if `uv.lock` no longer matches `pyproject.toml`.

**The creation hook.** The hook installs no Python packages. It does these steps:

1. It checks offline that `uv.lock` still matches `pyproject.toml`, and it fails if they differ. It never rewrites the lock.
2. It fetches GitHub's SSH host keys over HTTPS from `api.github.com/meta` and writes them to `~/.ssh/known_hosts`.
3. It installs Claude Code and standalone Codex with their native installers.

Sign in with `codex login` before you use the direct CLI review. [The review skill](.agents/skills/adversarial-review/SKILL.md#reviewer-session) owns the dispatch.

**Git over SSH.**

- The container mounts your `~/.gitconfig` read-only. It mounts no SSH keys.
- Git over SSH uses the SSH agent that VS Code forwards from the host.
- Load your key into the host's agent, for example with `ssh-add`, before you open the container. Then check with `ssh-add -l` inside it.

**The default configuration.**

- It needs no `.env` file.
- It never mounts the real staging share, the browser profile or the production database. It does bind-mount the workspace and your `~/.gitconfig`.
- Whatever `.env` sets, it gets a throwaway `dev_staging` volume as its staging area, its own `dev_db` database volume and no browser profile.

**The REAL DATA configuration.** The second configuration, **youtube-collector (REAL DATA)** (`.devcontainer/real-data/`), is for sessions that deliberately need the real staging share and cookies. VS Code offers it when you reopen in the container.

- It mounts `ASSETS_DIR` as the staging area and `FIREFOX_PROFILE_DIR` read-only. It refuses to start unless both are set.
- It sets `COLLECTOR_REAL_DATA=1`.
- It keeps the development database.
- Opening it authorizes nothing. Tests must still use only test-owned storage, and each run against real YouTube needs separate authorization ([AGENTS.md](AGENTS.md#safety)).

Both configurations are the same Compose service, so a switch between them recreates the container.

## Configuration

Compose reads these **interpolation** variables from your shell or from an optional, git-ignored `.env` file in the repository root.

| Variable | Default | Purpose |
| --- | --- | --- |
| `ASSETS_DIR` | `./assets` | Host directory bind-mounted as the staging area at `/workspaces/youtube-collector/assets`, by the service and the real-data dev container only. |
| `FIREFOX_PROFILE_DIR` | `firefox_profile` (named volume) | Host Firefox profile for yt-dlp cookies. See the notes below. |
| `APT_PROXY` | empty (none) | Optional apt caching proxy used during the image build. |
| `PYPI_PROXY` | empty (none) | Optional PyPI proxy for every PyPI package the image installs, including the dev container's dev tools. `update-dependencies.sh` still resolves against PyPI. |
| `BUILD_COMMON_CONTEXT` | pinned Git URL (`docker-build-common.git#v1.2.0`) | Shared build-helper source. See the notes below. |
| `COLLECTOR_PORT` | `8000` | Host port the service publishes on; the container listens on 8000. The service is unauthenticated, so publish it only on a trusted LAN. The dev containers publish no port. |

**`FIREFOX_PROFILE_DIR`.**

- Only the service and the real-data dev container mount the profile, and they mount it read-only.
- Set the variable to download private or age-gated videos.
- The cookies apply to media only. Captions are always fetched without them ([T39](tasks/open/T39.md)).

**`BUILD_COMMON_CONTEXT`.**

- A local clone avoids the fetch of that context.
- The build uses whatever the clone has checked out. Keep the clone at the tag that `docker-compose.yaml` pins.
- An older checkout can lack helpers that the Dockerfile calls. A newer checkout builds with untested helpers.
- Check the tag with `git -C <clone> describe --tags`.

**Volumes.**

- The `collector_db` named volume holds the service's SQLite database (`collector.sqlite3`). It is mounted at `/var/lib/youtube-collector`.
- `collector_db` lives in local Docker storage, not on the staging share. It survives container recreation, and `docker compose down -v` deletes it.
- The dev containers use `dev_db` instead and never mount `collector_db`.
- The dev containers also mount a `dev_persist` volume at `/home/ubuntu/persist`. It survives sessions and container rebuilds, and `docker compose down -v` deletes it too.
- `dev_persist` holds Claude Code's settings, history, memory and sign-in (`CLAUDE_CONFIG_DIR`, which also moves `~/.claude.json`). It also holds Codex's sessions and sign-in (`CODEX_HOME`).
- `dev_persist` holds the credentials of both tools, so treat it like your home directory.
- `dev_persist` stays on one machine. Only the Git repository carries work between machines.

**Compose project names.**

- The Compose service sets no fixed container name.
- The service has the project name `youtube-collector-service`, from the `docker-compose.yaml` in the repository root.
- Both dev container configurations have the project name `youtube-collector`, from their override.
- Therefore a `docker compose up` or `down` in the repository root leaves the dev container alone.
- Leave `COMPOSE_PROJECT_NAME` unset. A value in your shell or in `.env` would override both names.

The service reads its **runtime settings** from the environment.

- `docker-compose.yaml` sets the paths, and passes the pacing variables and the retention variable through from `.env`.
- An empty or unset variable takes its default.
- An invalid value stops the service at startup, with a message that names the variable.

| Variable | Default | Purpose |
| --- | --- | --- |
| `COLLECTOR_STAGING_DIR` | required; Compose sets `/workspaces/youtube-collector/assets` | The staging area inside the container. It must contain the marker file `.youtube-collector-root`, which you create once at the root of the share; without it all work pauses (DESIGN 5.13). |
| `COLLECTOR_DATABASE` | `/var/lib/youtube-collector/collector.sqlite3` | The SQLite file, on the `collector_db` volume. |
| `COLLECTOR_SLEEP_REQUESTS` | `0` | Seconds yt-dlp sleeps between requests during extraction. |
| `COLLECTOR_SLEEP_DOWNLOAD` | `0` | Seconds yt-dlp sleeps before each download, or the lower bound of a random range. |
| `COLLECTOR_MAX_SLEEP_DOWNLOAD` | unset | The upper bound of that range. |
| `COLLECTOR_JOB_RETENTION_DAYS` | `7` | Days a finished job stays readable. Expired jobs are deleted when the next job is submitted. |
| `COLLECTOR_HOST`, `COLLECTOR_PORT` | `0.0.0.0`, `8000` | Where the server listens inside the container; Compose does not pass these through. |
| `COLLECTOR_SHUTDOWN_GRACE` | `30` | Seconds that shutdown waits for the current download before it abandons the download. See the notes below. |

**`COLLECTOR_SHUTDOWN_GRACE`.**

- The wait does not stop the download. The download ends with the process, and restart recovery requeues it.
- Compose does not pass this variable through.
- Keep the service's `stop_grace_period` (45 s) above this value plus 5 s, so that normal shutdowns finish first.
- Change both values together in `docker-compose.yaml`.

## Running the service

1. Create the staging marker once: `touch <share>/.youtube-collector-root`.
2. Build and start the service from the repository root on the host. The dev container has no Docker.

```sh
docker compose build
docker compose up -d
curl http://localhost:8000/health   # 200 when the database, staging marker and worker are healthy, 503 otherwise
```

**Use.**

- The interactive API documentation is at `http://<host>:8000/docs`.
- Submit work with `POST /jobs/videos`, `/jobs/captions` or `/jobs/videos-with-captions`.
- Follow a job by polling `GET /jobs/{id}`.
- Check errored tasks with `GET /failures`.
- [DESIGN section 6](DESIGN.md#6-http-api-summary) lists every endpoint.
- A running service downloads from YouTube with your cookies, so the decision to start it is yours. Agents need your authorization for each run ([AGENTS.md](AGENTS.md#safety)).

**Shutdown.**

- `docker compose stop` gives open requests up to 5 seconds. It then lets the current download finish for up to 30 seconds.
- An abandoned download restarts from scratch on the next start.
- This shutdown is graceful for normal operation, but it is not a guarantee. An exceptional stall, such as a blocked database operation, can keep the process alive until Docker kills it after the 45-second stop grace.
- Restart recovery applies in both cases.
- A download that has not finished when the process ends leaves a hidden `.staging-*` directory on the share, with its partial file. This holds for an abandoned download and for a killed process.
- A restart does not remove that directory. Delete such directories by hand while the service is stopped ([DESIGN 5.12](DESIGN.md#512-restart-recovery)).

**When the worker fails.**

- An error that the worker cannot handle, such as a database failure while it saves a result, makes the process exit with status 1 after 60 seconds. The log names the error.
- During those 60 seconds the API still answers, and `/health` answers 503 with `threads.worker` false.
- Docker then restarts the service. Restart recovery requeues the interrupted work, and a file that is already published counts as `existing`.
- An item or a playlist expansion that has been interrupted three times fails with an `internal` failure instead. Restarts with `docker compose restart` count too. Resubmit the job when the cause is fixed.
- A database that stays broken gives one restart about every minute. The log of each turn shows the cause ([DESIGN 5.12](DESIGN.md#512-restart-recovery)).

## Updating dependencies

- `pyproject.toml` declares the dependencies: runtime dependencies under `[project]`, and dev tools in the `dev` dependency group.
- `uv.lock` pins the full resolution. This includes the artifact hashes and the `pathlib-extensions` commit.
- The lock changes only through deliberate commands: `uv lock`, the update script below, or `uv add`. A rebuild of the container never changes it.

Refresh the lock from the dev container with the update script. The script locks, and then applies the new lock to the container's venv with `uv sync --locked`.

```sh
./update-dependencies.sh                          # move every package to the newest version pyproject.toml allows
./update-dependencies.sh yt-dlp                   # move only the named packages
./update-dependencies.sh pathlib-extensions       # bump the git dependency to its latest commit
```

- yt-dlp breaks whenever YouTube changes, and it ships fixes often. `./update-dependencies.sh yt-dlp` is the routine way to update it alone.
- To add a runtime dependency, use `uv add <package>`. To add a dev tool, use `uv add --group dev <package>`. Both update `pyproject.toml`, `uv.lock` and the venv together.
- The script talks to PyPI directly, because a lock resolved through the mirror would record the mirror's URLs. A rebuild of the dev container then installs the new lock through the mirror.

After an update:

1. Review the `uv.lock` diff.
2. Run the gates under [Testing](#testing).
3. Commit `pyproject.toml` and `uv.lock` together.
4. Rebuild the image to deploy the new lock. The image installs only what the lock records.

## Testing

[AGENTS.md](AGENTS.md#validation-and-review) owns when the gates are required. [tests/README.md](tests/README.md) owns how tests are written. The gates are:

```sh
./check-coverage.sh
mypy youtube_collector
ruff check .
```

**The coverage gate.**

- `check-coverage.sh` runs pytest. It requires at least 80% statement coverage and branch coverage, for every measured file and for the project.
- Run it from the repository root.
- It refuses to run unless `youtube_collector` resolves to this tree's package.
- It also refuses to run unless `python3` matches the version that `.python-version` pins. A pin of `3.14` accepts any 3.14.x.
- Without that check, a dev container built before a Python upgrade would pass the suite on its old interpreter. Rebuild the container when the check fails.
- `COVERAGE_THRESHOLD` overrides the script's threshold with a number from 0 to 100 inclusive. An empty value keeps 80.
- A value that is not numeric, not finite or out of range fails before pytest runs.
- pytest also keeps its configured aggregate floor of 80%.
- Pass only whole-suite options to this gate, such as `-x` or `-q`. Selected test paths cannot certify per-file coverage.
- Coverage includes the application package and the local mutation and worktree gate helpers. The shared repository measures coverage of its own tools.

**During development.**

- Tests run in random order through `pytest-randomly`. Reproduce an ordering failure with the printed `--randomly-seed=<seed>`.
- Use focused checks: `pytest --no-cov <paths>`. The option avoids the aggregate coverage failure that a partial selection causes.
- Inspect the per-file coverage and the warnings in the final run.

**A separate worktree.**

- Run the gates in the tree that is validated.
- In a separate git worktree, set `PYTHONPATH` to the worktree's root. A worktree that holds a frozen candidate for review is an example.
- Without that setting, the dev container's editable install supplies the main tree's package. `check-coverage.sh` then refuses to run, because it would measure the wrong files.

**Checks that need Docker.**

- The dev container has no Docker daemon. Image builds, container creation and runs of the service are therefore host checks.
- Record host checks in the host-check task, [T10](tasks/open/T10.md).
- `docker compose config` needs no daemon. To validate Compose changes in the dev container, do these steps:
  1. Download the standalone Compose binary for the session from the Compose GitHub release.
  2. Check the binary against the SHA-256 that the release publishes.
  3. Run it in a scratch copy of the repository without `.env`, with `COMPOSE_PROJECT_NAME` unset.

**The documentation check.** [AGENTS.md](AGENTS.md#validation-and-review) requires it for every change that touches a Markdown file.

- Run `../agent-workflow-common/tools/check_docs.py` from the repository. It takes less than a second.
- It checks that each relative link and each anchor resolves.
- It checks the task files against the index in `tasks/TODO.md`.
- It checks the rules of [STYLE.md](STYLE.md) that need no judgment, such as the sentence limits.
- In `DESIGN.md`, `project-reviews.md` and the task files, it checks the style of changed lines only.
- It prints each problem as `path:line: rule: message`.
- A pass means that the local documentation references resolve. It does not mean that the documents agree with each other.

**The reference report.** It is optional. Use it before you rename or move a file or a heading.

- Run `../agent-workflow-common/tools/check_docs.py --references AGENTS.md` to list each line that links to that file or to one of its headings.
- Run `../agent-workflow-common/tools/check_docs.py --references 'AGENTS.md#governance-files'` to list only the links to that heading.
- Write the path from the root of the repository.
- The report runs no check. It lists Markdown links only, not paths in code or in plain text.

**Tool configuration.** `pyproject.toml` configures the tools.

- pytest rejects unregistered markers and unexpected passes of `xfail` tests.
- pytest turns every warning into a test failure. Assert an expected warning with `pytest.warns`.
- Ruff enforces pycodestyle (120-character lines), Pyflakes, import order, bugbear, pyupgrade and Google-style docstrings.
- mypy reports missing imports as errors.
- `pathlib_extensions` and `yt_dlp` ship no type information, so `pyproject.toml` gives them an `ignore_missing_imports` override.
- Tests are intentionally outside the mypy gate.

## Shared tools

Three tools run from the sibling checkout `../agent-workflow-common`. This repository holds their configuration in `agent-workflow.json` and keeps no copies of their implementations or tests.

| Tool | Purpose |
|---|---|
| `../agent-workflow-common/tools/check_docs.py` | Documentation checks and reference reports |
| `../agent-workflow-common/tools/snapshot.py` | Review integrity snapshots |
| `../agent-workflow-common/tools/dispatch.py` | Codex CLI reviewer dispatch |

- Both devcontainers run `.devcontainer/postCreateCommand.sh`, which clones the tools into `$HOME/persist/agent-workflow-common` on the existing `dev_persist` volume.
- The hook changes only `/workspaces` ownership to `ubuntu:ubuntu` with `sudo chown`, then links the checkout at `/workspaces/agent-workflow-common`.
- First setup needs GitHub access. Existing checkouts are reused without pulls or resets, so local work survives recreation of the container.
- Setup stops if the sibling path conflicts, the checkout is invalid, required tools are missing, or cloning fails. It preserves existing paths.
- Resolve a reported conflict explicitly. The hook never replaces another checkout or removes an incomplete one.
- This checkout is separate from ClipboardTTS's checkout. Commit and push shared changes to transfer them between environments.
- Temporary review evidence stays outside the repository. Governance, documentation and durable review records remain tracked but are excluded from Docker's build context.
- The tools use Python 3.9 or later and the standard library. Their `--help` text describes the arguments.
- The checkout determines the tool version. No version is pinned, matching ClipboardTTS.
- Change shared tools and run their tests in their repository. Verify both consumers after shared changes.
- Keep `mutate.py` and `worktree_gates.py` local. They implement this project's Python test isolation and gates.
- From a review worktree, use the shared checkout's absolute tool path and run it with the worktree as the current directory.
- For CLI dispatch from another directory, pass the worktree with `--target`.
- [project-reviews.md](project-reviews.md) preserves historical project reviews. Task state remains in [tasks/](tasks/).
