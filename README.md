# youtube-collector

Download YouTube videos, playlists, and captions into a staging area, as a LAN-only HTTP service. [DESIGN.md](DESIGN.md) owns the requirements and design; [tasks/TODO.md](tasks/TODO.md) owns outstanding work; [AGENTS.md](AGENTS.md) owns working standards.

**Status:** the service is implemented and tested against fakes: the HTTP API and the YouTube worker thread run in one process ([DESIGN section 14](DESIGN.md#14-architecture)). It has not yet been run against real YouTube (task [T25](tasks/open/T25.md)), and the container-level checks are listed in task [T10](tasks/open/T10.md). The sections below cover building, configuring, running and testing it.

## Security / trust model

The service is unauthenticated and intended for a trusted LAN only. Do not expose it to the public internet. See [DESIGN.md](DESIGN.md#7-deployment-and-operation).

## Dev Container (recommended)

Open the repository in VS Code and reopen it in the Dev Container (`.devcontainer/`). The container's `app` service builds the Dockerfile's `dev` target (`.devcontainer/docker-compose.devcontainer.yaml`), which installs the locked runtime and `dev` dependencies and the editable project into the venv at `/opt/venv`, first on `PATH`. Every PyPI package is fetched through `PYPI_PROXY` when it is set. The image is Ubuntu 26.04 with its distro Python 3.14, and also contains ffmpeg, Deno (yt-dlp's JavaScript runtime), uv, git and curl. The service's `runtime` target keeps only ffmpeg, Deno, Python and the venv, with the project installed as a regular package. The build fails if `uv.lock` no longer matches `pyproject.toml`.

The creation hook installs no Python packages. It checks offline that `uv.lock` still matches `pyproject.toml` and fails if it doesn't, without ever rewriting the lock. It writes GitHub's SSH host keys, fetched over HTTPS from `api.github.com/meta`, to `~/.ssh/known_hosts`. It then installs Claude Code and standalone Codex with their native installers and verifies `codex --version`. Sign in with `codex login` before using direct CLI review; [the review skill](.agents/skills/adversarial-review/SKILL.md#reviewer-session) owns dispatch and authentication checks.

The container mounts your `~/.gitconfig` read-only but no SSH keys. Git over SSH uses the SSH agent that VS Code forwards from the host, so load your key into the host's agent (for example `ssh-add`) before opening the container, then check with `ssh-add -l` inside it.

No `.env` file is required, and the default configuration never mounts the real staging share, browser profile or production database (it does bind-mount the workspace and your `~/.gitconfig`): whatever `.env` sets, it gets a throwaway `dev_staging` volume as its staging area, its own `dev_db` database volume and no browser profile. The second configuration, **youtube-collector (REAL DATA)** (`.devcontainer/real-data/`), is for sessions that deliberately need the real staging share and cookies; VS Code offers it when you reopen in the container. It mounts `ASSETS_DIR` as the staging area and `FIREFOX_PROFILE_DIR` read-only, refuses to start unless both are set, sets `COLLECTOR_REAL_DATA=1`, and keeps the development database. Opening it authorizes nothing: tests must still use only test-owned storage, and each run against real YouTube needs separate authorization ([AGENTS.md](AGENTS.md#safety)). Both configurations are the same Compose service, so switching between them recreates the container.

## Configuration

Compose reads these **interpolation** variables from your shell or an optional, git-ignored `.env` file in the repository root.

| Variable | Default | Purpose |
| --- | --- | --- |
| `ASSETS_DIR` | `./assets` | Host directory bind-mounted as the staging area at `/workspaces/youtube-collector/assets`, by the service and the real-data dev container only. |
| `FIREFOX_PROFILE_DIR` | `firefox_profile` (named volume) | Host Firefox profile for yt-dlp cookies, mounted read-only by the service and the real-data dev container only; set it to download private or age-gated videos. |
| `APT_PROXY` | empty (none) | Optional apt caching proxy used during the image build. |
| `PYPI_PROXY` | empty (none) | Optional PyPI proxy for every PyPI package the image installs, including the dev container's dev tools. `update-dependencies.sh` still resolves against PyPI. |
| `BUILD_COMMON_CONTEXT` | pinned Git URL (`docker-build-common.git#v1.2.0`) | Shared build-helper source; a local clone avoids fetching that context. The build uses whatever the clone has checked out, so keep it at the tag `docker-compose.yaml` pins: an older checkout can lack helpers the Dockerfile calls, and a newer one builds with untested helpers. Check with `git -C <clone> describe --tags`. |
| `COLLECTOR_PORT` | `8000` | Host port the service publishes on; the container listens on 8000. The service is unauthenticated, so publish it only on a trusted LAN. The dev containers publish no port. |

The `collector_db` named volume, mounted at `/var/lib/youtube-collector`, holds the service's SQLite database (`collector.sqlite3`). It lives in local Docker storage rather than the staging share, and survives container recreation. `docker compose down -v` deletes it. The dev containers use `dev_db` instead and never mount `collector_db`. They also mount a `dev_persist` volume at `/home/ubuntu/persist`, which survives sessions and container rebuilds; `docker compose down -v` deletes it too. It holds Claude Code's settings, history, memory and sign-in (`CLAUDE_CONFIG_DIR`, which also moves `~/.claude.json`), and Codex's sessions and sign-in (`CODEX_HOME`). Because it holds both tools' credentials, treat it like your home directory. It stays on one machine: only the Git repository carries work between machines. The Compose service sets no fixed container name, and the two have different Compose project names: `youtube-collector-service` for the service, from the repository root's `docker-compose.yaml`, and `youtube-collector` for both dev container configurations, from their override. A `docker compose up` or `down` in the repository root therefore leaves the dev container alone. Setting `COMPOSE_PROJECT_NAME` in your shell or `.env` would override both names, so leave it unset.

The service reads its **runtime settings** from the environment; `docker-compose.yaml` sets the paths and passes the pacing and retention variables through from `.env`. An empty or unset variable takes its default, and an invalid value stops the service at startup with a message naming it.

| Variable | Default | Purpose |
| --- | --- | --- |
| `COLLECTOR_STAGING_DIR` | required; Compose sets `/workspaces/youtube-collector/assets` | The staging area inside the container. It must contain the marker file `.youtube-collector-root`, which you create once at the root of the share; without it all work pauses (DESIGN 5.13). |
| `COLLECTOR_DATABASE` | `/var/lib/youtube-collector/collector.sqlite3` | The SQLite file, on the `collector_db` volume. |
| `COLLECTOR_SLEEP_REQUESTS` | `0` | Seconds yt-dlp sleeps between requests during extraction. |
| `COLLECTOR_SLEEP_DOWNLOAD` | `0` | Seconds yt-dlp sleeps before each download, or the lower bound of a random range. |
| `COLLECTOR_MAX_SLEEP_DOWNLOAD` | unset | The upper bound of that range. |
| `COLLECTOR_JOB_RETENTION_DAYS` | `7` | Days a finished job stays readable. Expired jobs are deleted when the next job is submitted. |
| `COLLECTOR_HOST`, `COLLECTOR_PORT` | `0.0.0.0`, `8000` | Where the server listens inside the container; Compose does not pass these through. |
| `COLLECTOR_SHUTDOWN_GRACE` | `30` | Seconds shutdown waits for the current download before abandoning it; restart recovery requeues it. Compose does not pass it through: the service's `stop_grace_period` (45 s) must stay above it plus 5 s, so change both in `docker-compose.yaml` together. |

## Running the service

Create the staging marker once (`touch <share>/.youtube-collector-root`), then build and start the service from the repository root on the host, since the dev container has no Docker:

```sh
docker compose build
docker compose up -d
curl http://localhost:8000/health   # 200 when the database, staging marker and worker are healthy, 503 otherwise
```

The interactive API documentation is at `http://<host>:8000/docs`. Submit work with `POST /jobs/videos`, `/jobs/captions` or `/jobs/videos-with-captions`, follow it by polling `GET /jobs/{id}`, and check errored tasks with `GET /failures`; [DESIGN section 6](DESIGN.md#6-http-api-summary) lists every endpoint. A running service downloads from YouTube with your cookies, so starting it is your decision; agents need your authorization for each run ([AGENTS.md](AGENTS.md#safety)). `docker compose stop` lets the current download finish for up to 30 seconds; an abandoned download restarts from scratch on the next start.

## Updating dependencies

`pyproject.toml` declares dependencies: runtime ones under `[project]` and dev tools in the `dev` dependency group. `uv.lock` pins the full resolution, including artifact hashes and the `pathlib-extensions` commit. The lock changes only through deliberate commands (`uv lock`, the update script below, or `uv add`); rebuilding the container never changes it. Refresh it from the dev container with the update script, which locks and then applies the new lock to the container's venv with `uv sync --locked`:

```sh
./update-dependencies.sh                          # move every package to the newest version pyproject.toml allows
./update-dependencies.sh yt-dlp                   # move only the named packages
./update-dependencies.sh pathlib-extensions       # bump the git dependency to its latest commit
```

yt-dlp breaks whenever YouTube changes and ships fixes often, so `./update-dependencies.sh yt-dlp` is the routine way to update it alone. To add a dependency, use `uv add <package>` for runtime or `uv add --group dev <package>` for a dev tool; it updates `pyproject.toml`, `uv.lock` and the venv together.

Then review the `uv.lock` diff, run the gates under [Testing](#testing), commit `pyproject.toml` and `uv.lock` together, and rebuild the image to deploy the new lock. The image installs only what the lock records; both its build and the dev container's creation hook check offline that the lock still matches `pyproject.toml`, and fail if it doesn't. The script talks to PyPI directly, because a lock resolved through the mirror would record the mirror's URLs; rebuilding the dev container then installs the new lock through the mirror.

## Testing

For non-trivial code, test or configuration changes, run the required gates before commit; configuration-only changes, such as Dockerfile, Compose or devcontainer edits, need them too. [AGENTS.md](AGENTS.md#validation-and-review) owns when they are required and the documentation-only exemption; [tests/README.md](tests/README.md) owns how tests are written:

```sh
./check-coverage.sh
mypy youtube_collector
ruff check .
```

`check-coverage.sh` runs pytest and requires at least 80% statement and branch coverage for every measured file and the project. Run it from the repository root: it first refuses to run unless `python3` matches the version `.python-version` pins (`3.14` accepts any 3.14.x), because a dev container built before a Python upgrade would otherwise pass the suite on its old interpreter; rebuild the container when that happens. `COVERAGE_THRESHOLD` overrides the script threshold with a number from 0 to 100 inclusive; an empty value keeps 80, and any non-numeric, non-finite or out-of-range value fails before pytest runs. pytest also keeps its configured 80% aggregate floor. Pass only whole-suite options such as `-x` or `-q` to this gate; selected test paths cannot certify per-file coverage. Coverage includes the application package and the standalone [review snapshot helper](.agents/skills/adversarial-review/references/snapshot.py), whose tests load it by file path. Tests are intentionally outside the mypy gate.

Tests run in random order through `pytest-randomly`; reproduce an ordering failure with the printed `--randomly-seed=<seed>`. During development, use focused checks: `pytest --no-cov <paths>` avoids the aggregate coverage failure caused by selecting only part of the suite. Inspect per-file coverage and warnings in the final run. Tests must not reach YouTube or host data; [AGENTS.md](AGENTS.md#tests) owns these protections.

Run the gates in the tree being validated. In a separate git worktree, such as one a candidate is frozen in for review, set `PYTHONPATH` to the worktree's root and confirm that `youtube_collector` is imported from it (`python3 -c 'import youtube_collector; print(youtube_collector.__file__)'`): the dev container's editable install otherwise imports the main tree's package, and the gate measures the wrong files.

The dev container has no Docker daemon, so image builds, container creation and running the service are host checks: record them in the host-check task, [T10](tasks/open/T10.md), and label any simulated result as simulated. `docker compose config` needs no daemon. To validate Compose changes here, download the standalone Compose binary for the session from the Compose GitHub release, check it against the release's published SHA-256, and run it in a scratch copy of the repository without `.env`, with `COMPOSE_PROJECT_NAME` unset.

The tools are configured in `pyproject.toml`. pytest rejects unregistered markers and unexpected passes of `xfail` tests, and turns every warning into a test failure: assert an expected warning with `pytest.warns`. Ruff enforces pycodestyle (120-character lines), Pyflakes, import order, bugbear, pyupgrade and Google-style docstrings. mypy reports missing imports as errors. `pathlib_extensions` and `yt_dlp` ship no type information, so `pyproject.toml` gives them an `ignore_missing_imports` override.
