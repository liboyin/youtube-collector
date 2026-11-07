# youtube-collector

Download YouTube videos, playlists, and captions into a staging area, as a LAN-only HTTP service. [DESIGN.md](DESIGN.md) owns the requirements and design; [TODO.md](TODO.md) owns outstanding work; [AGENTS.md](AGENTS.md) owns working standards.

**Status:** the development environment exists, but the service itself is not implemented yet. The sections below cover building, configuring and testing the repository.

## Security / trust model

The planned service is unauthenticated and intended for a trusted LAN only. Do not expose it to the public internet. See [DESIGN.md](DESIGN.md#7-deployment-and-operation).

## Dev Container (recommended)

Open the repository in VS Code and reopen it in the Dev Container (`.devcontainer/`). The container's `app` service builds the Dockerfile's `dev` target (`.devcontainer/docker-compose.devcontainer.yaml`), which installs the locked runtime and `dev` dependencies and the editable project into the venv at `/opt/venv`, first on `PATH`. Every PyPI package is fetched through `PYPI_PROXY` when it is set. The image is Ubuntu 26.04 with its distro Python 3.14, and also contains ffmpeg, Deno (yt-dlp's JavaScript runtime), uv, git and curl. The service's `runtime` target keeps only ffmpeg, Deno, Python and the venv, with the project installed as a regular package. The build fails if `uv.lock` no longer matches `pyproject.toml`.

The creation hook installs no Python packages. It checks offline that `uv.lock` still matches `pyproject.toml` and fails if it doesn't, without ever rewriting the lock. It writes GitHub's SSH host keys, fetched over HTTPS from `api.github.com/meta`, to `~/.ssh/known_hosts`. It then installs Claude Code and standalone Codex with their native installers and verifies `codex --version`. Sign in with `codex login` before using direct CLI review; [the review skill](.agents/skills/adversarial-review/SKILL.md#reviewer-session) owns dispatch and authentication checks.

The container mounts your `~/.gitconfig` read-only but no SSH keys. Git over SSH uses the SSH agent that VS Code forwards from the host, so load your key into the host's agent (for example `ssh-add`) before opening the container, then check with `ssh-add -l` inside it.

No `.env` file is required, and the default configuration never mounts host data: whatever `.env` sets, it gets a throwaway `dev_staging` volume as its staging area, its own `dev_db` database volume and no browser profile. The second configuration, **youtube-collector (REAL DATA)** (`.devcontainer/real-data/`), is for sessions that deliberately need the real staging share and cookies; VS Code offers it when you reopen in the container. It mounts `ASSETS_DIR` as the staging area and `FIREFOX_PROFILE_DIR` read-only, refuses to start unless both are set, sets `COLLECTOR_REAL_DATA=1`, and keeps the development database. Opening it authorizes nothing: tests must still use only test-owned storage, and each run against real YouTube needs separate authorization ([AGENTS.md](AGENTS.md#validation-and-review)). Both configurations are the same Compose service, so switching between them recreates the container.

## Configuration

Compose reads these **interpolation** variables from your shell or an optional, git-ignored `.env` file in the repository root. The service's own runtime settings will be added with the application.

| Variable | Default | Purpose |
| --- | --- | --- |
| `ASSETS_DIR` | `./assets` | Host directory bind-mounted as the staging area at `/workspaces/youtube-collector/assets`, by the service and the real-data dev container only. |
| `FIREFOX_PROFILE_DIR` | `firefox_profile` (named volume) | Host Firefox profile for yt-dlp cookies, mounted read-only by the service and the real-data dev container only; set it to download private or age-gated videos. |
| `APT_PROXY` | empty (none) | Optional apt caching proxy used during the image build. |
| `PYPI_PROXY` | empty (none) | Optional PyPI proxy for every PyPI package the image installs, including the dev container's dev tools. `update-dependencies.sh` still resolves against PyPI. |
| `BUILD_COMMON_CONTEXT` | pinned Git URL (`docker-build-common.git#v1.2.0`) | Shared build-helper source; a local clone avoids fetching that context. The build uses whatever the clone has checked out, so keep it at the tag `docker-compose.yaml` pins: an older checkout can lack helpers the Dockerfile calls, and a newer one builds with untested helpers. Check with `git -C <clone> describe --tags`. |

The `collector_db` named volume, mounted at `/var/lib/youtube-collector`, will hold the service's SQLite database. It lives in local Docker storage rather than the staging share, and survives container recreation. `docker compose down -v` deletes it. The dev containers use `dev_db` instead and never mount `collector_db`. They also mount a `dev_persist` volume at `/home/ubuntu/persist`, which survives sessions and container rebuilds; `docker compose down -v` deletes it too. It holds review working records and evidence (`COLLECTOR_EVIDENCE_DIR`), Claude Code's settings, history, memory and sign-in (`CLAUDE_CONFIG_DIR`, which also moves `~/.claude.json`), and Codex's sessions and sign-in (`CODEX_HOME`). Because it holds both tools' credentials, treat it like your home directory. The Compose service sets no fixed container name, so the dev container and the service can run on the same Docker daemon.

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

For non-trivial code, test or configuration changes, run the required gates before commit. [AGENTS.md](AGENTS.md#validation-and-review) owns baseline selection and documentation/cosmetic exceptions:

```sh
./check-coverage.sh
mypy youtube_collector
ruff check .
```

`check-coverage.sh` runs pytest and requires at least 80% statement and branch coverage for every measured file and the project. Run it from the repository root: it first refuses to run unless `python3` matches the version `.python-version` pins (`3.14` accepts any 3.14.x), because a dev container built before a Python upgrade would otherwise pass the suite on its old interpreter; rebuild the container when that happens. `COVERAGE_THRESHOLD` overrides the script threshold with a number from 0 to 100 inclusive; an empty value keeps 80, and any non-numeric, non-finite or out-of-range value fails before pytest runs. pytest also keeps its configured 80% aggregate floor. Pass only whole-suite options such as `-x` or `-q` to this gate; selected test paths cannot certify per-file coverage. Coverage includes the application package and the standalone [review snapshot helper](.agents/skills/adversarial-review/references/snapshot.py), whose tests load it by file path. Tests are intentionally outside the mypy gate.

Tests run in random order through `pytest-randomly`; reproduce an ordering failure with the printed `--randomly-seed=<seed>`. For focused checks, use `pytest --no-cov` to avoid the aggregate coverage failure caused by selecting only part of the suite. Tests must not reach YouTube or host data; [AGENTS.md](AGENTS.md#test-guidelines) owns these protections.

The tools are configured in `pyproject.toml`. pytest rejects unregistered markers and unexpected passes of `xfail` tests, and turns every warning into a test failure: assert an expected warning with `pytest.warns`. Ruff enforces pycodestyle (120-character lines), Pyflakes, import order, bugbear, pyupgrade and Google-style docstrings. mypy reports missing imports as errors; `yt_dlp` and `pathlib_extensions` ship no type information, so the change that first imports one adds the `ignore_missing_imports` override described in `pyproject.toml`.
