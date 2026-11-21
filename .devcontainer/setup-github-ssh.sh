#!/bin/bash
set -euo pipefail

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
