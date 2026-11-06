#! /bin/bash
# Refresh uv.lock deliberately and apply it to the dev container's venv. This is the only routine
# way the lock changes: rebuilding the container never rewrites it (see README, "Updating
# dependencies").
#
# Usage: ./update-dependencies.sh [package...]
#   With no packages, every locked package moves to the newest version pyproject.toml allows.
#   With packages, only those move (plus whatever their new versions require), e.g. yt-dlp, or
#   pathlib-extensions to bump its git commit.
#
# uv prints each version change. Afterwards review `git diff uv.lock`, run the gates, and commit
# pyproject.toml and uv.lock together; the image picks up the new lock on its next build.
#
# Runs from the repository root wherever it is invoked from, and talks to PyPI directly: a lock
# resolved through the PYPI_PROXY mirror would record the mirror's URLs (README.md, "Updating
# dependencies").
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

upgrade_args=(--upgrade)
if [ "$#" -gt 0 ]; then
    upgrade_args=()
    for package in "$@"; do
        # Without this check an option such as --frozen would be passed to uv as a package name
        # or, worse, change what the lock command does.
        if [[ "$package" == -* ]]; then
            echo "update-dependencies: expected package names, got option '$package'" >&2
            exit 2
        fi
        upgrade_args+=(--upgrade-package "$package")
    done
fi

uv lock "${upgrade_args[@]}"
# --locked confirms the lock just written matches pyproject.toml before changing the venv.
uv sync --locked
