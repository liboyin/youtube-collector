#! /bin/bash
set -euo pipefail
# The image's dev target already installed the locked runtime and dev dependencies through
# PYPI_PROXY, so nothing is installed here. Only confirm that uv.lock still matches
# pyproject.toml: --offline keeps this check off the network, and nothing here rewrites the
# lock, because refreshing it is a deliberate ./update-dependencies.sh. An up-to-date lock is
# confirmed from the files alone; a stale one fails, with uv's message about being unable to
# fetch the git dependency offline.
if ! uv lock --check --offline; then
    echo "postCreateCommand: uv.lock does not match pyproject.toml; run 'uv lock' and then 'uv sync --locked'" >&2
    exit 1
fi
# Git over SSH authenticates through the agent VS Code forwards; no ~/.ssh is mounted (DESIGN
# decision C31). Record GitHub's published host keys, fetched over verified HTTPS, so the
# first non-interactive fetch or push neither prompts nor trusts an unverified key.
install -d -m 700 "$HOME/.ssh"
curl -fsSL https://api.github.com/meta | python3 -c '
import json, sys
keys = json.load(sys.stdin)["ssh_keys"]
if not keys:
    sys.exit("postCreateCommand: GitHub metadata listed no SSH host keys")
print("\n".join(f"github.com {key}" for key in keys))
' > "$HOME/.ssh/known_hosts"
# use native installer of Claude Code
curl -fsSL https://claude.ai/install.sh | bash
# Install standalone Codex for direct CLI review, without prompting or launching a session.
curl -fsSL https://chatgpt.com/codex/install.sh | CODEX_NON_INTERACTIVE=1 sh
# Both installers use this directory; this non-interactive shell has not picked it up.
export PATH="$HOME/.local/bin:$PATH"
codex --version
