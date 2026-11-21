#! /bin/bash
set -euo pipefail
# The image's dev target already installed the locked runtime and dev dependencies through
# PYPI_PROXY, so no Python dependencies are installed here. Confirm that uv.lock still matches
# pyproject.toml: --offline keeps this check off the network, and nothing here rewrites the
# lock, because refreshing it is a deliberate ./update-dependencies.sh. An up-to-date lock is
# confirmed from the files alone; a stale one fails, with uv's message about being unable to
# fetch the git dependency offline.
if ! uv lock --check --offline; then
    echo "postCreateCommand: uv.lock does not match pyproject.toml; run 'uv lock' and then 'uv sync --locked'" >&2
    exit 1
fi
# Directories on the dev_persist volume, which outlive the container; the Compose override sets
# these variables. They must exist before the installers below write Claude Code and Codex state.
for state_dir in "${CLAUDE_CONFIG_DIR:?}" "${CODEX_HOME:?}"; do
    install -d -m 700 "$state_dir"
done
bash .devcontainer/setup-github-ssh.sh
# /workspaces can belong to root on a new container. Change only its own ownership,
# not the mounted project or any other checkout. Keep the shared tools on dev_persist.
sudo chown ubuntu:ubuntu /workspaces
bash .devcontainer/setup-agent-tools.sh "$HOME/persist/agent-workflow-common" /workspaces/agent-workflow-common
# use native installer of Claude Code
curl -fsSL https://claude.ai/install.sh | bash
# Install standalone Codex for direct CLI review, without prompting or launching a session.
curl -fsSL https://chatgpt.com/codex/install.sh | CODEX_NON_INTERACTIVE=1 sh
# Both installers use this directory; this non-interactive shell has not picked it up.
export PATH="$HOME/.local/bin:$PATH"
