#!/bin/bash
# Initialize the persistent shared-tool checkout and expose it at the sibling path.
# Arguments are the checkout and link paths. Existing work is never updated or replaced.
set -euo pipefail
checkout="${1:?expected persistent checkout path}"
link="${2:?expected sibling link path}"

# Check the destination before cloning, including dangling links that -e would miss.
if [[ -e "$link" || -L "$link" ]]; then
    if [[ ! "$link" -ef "$checkout" ]]; then
        echo "setup-agent-tools: $link already exists and does not point to $checkout; preserving it" >&2
        exit 1
    fi
fi
if [[ ! -e "$checkout" && ! -L "$checkout" ]]; then
    git clone https://github.com/liboyin/agent-workflow-common.git "$checkout"
fi
# Reuse a valid checkout, including local edits. Do not pull, reset or remove it.
if [[ ! -e "$checkout/.git" ]] || ! git -C "$checkout" rev-parse --is-inside-work-tree > /dev/null; then
    echo "setup-agent-tools: $checkout is not a Git checkout; preserving it" >&2
    exit 1
fi
for tool in check_docs.py snapshot.py dispatch.py; do
    if [[ ! -f "$checkout/tools/$tool" ]]; then
        echo "setup-agent-tools: $checkout/tools/$tool is missing; update or repair the checkout explicitly" >&2
        exit 1
    fi
done
if [[ ! -e "$link" && ! -L "$link" ]]; then
    ln -sT "$checkout" "$link"
fi
