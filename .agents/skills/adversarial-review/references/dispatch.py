#!/usr/bin/env python3
"""Dispatch the adversarial reviewer through the Codex CLI, or continue a recorded one.

The base revision is read from the target when the prompt is written, never remembered. Standard
input is closed, because the CLI otherwise waits for more prompt text. The raw event stream goes to
``--log``; the session ID and the reviewer's report are printed.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

MODEL = 'gpt-6.1-sol'
EFFORT = 'medium'
_SKILL = '.agents/skills/adversarial-review/SKILL.md'
_RULES = (
    'The repository is read-only for you; run anything that needs writable state in a disposable scratch copy '
    "under the skill's rules, and never contact YouTube or use .env, browser profiles or the staging share."
)
_IDENTITY = (
    'Your CLI cannot observe its own model or effort; the dispatch arguments are '
    f"{MODEL} and {EFFORT}. Return the skill's report as your final message."
)


def base_revision(target: Path) -> str:
    """Return the target's current ``HEAD`` revision."""
    return subprocess.run(
        ['git', '-C', str(target), 'rev-parse', 'HEAD'], check=True, capture_output=True, text=True,
    ).stdout.strip()


def first_prompt(target: Path, boundary: str, record: str, purpose: str, scope: str, ignored: list[str]) -> str:
    """Return the prompt for a first assessment, supplying only what the review skill allows."""
    ignore = f"Unrelated paths to ignore: {', '.join(ignored)}.\n" if ignored else ''
    return (
        f'Read {target / _SKILL} and perform step 3 (Investigate and report) as the independent reviewer. {_RULES}\n\n'
        f'Repository: {target}\nTarget: the working tree, base revision {base_revision(target)}.\n'
        f'In-scope paths: {scope}.\n{ignore}Purpose: {purpose}\nAccepted boundary: {boundary}\n'
        f'Working record with the gate results and mutation evidence: {record}\n\n{_IDENTITY}'
    )


def follow_up_prompt(target: Path, record: str, delta: str) -> str:
    """Return the prompt for a follow-up on a recorded reviewer's earlier assessment."""
    return (
        f'Follow-up on the same rules; the base revision is now {base_revision(target)}. Delta: {delta}\n'
        f'Evidence: {record}\nAssess the delta and your earlier findings, and return an updated report and verdict.'
    )


def codex_command(prompt: str, session: str | None) -> list[str]:
    """Return the Codex CLI command for a first assessment, or for resuming ``session``."""
    options = [
        '--dangerously-bypass-approvals-and-sandbox', '--model', MODEL, '-c', f'model_reasoning_effort="{EFFORT}"',
        '--json',
    ]
    if session is None:
        return ['codex', 'exec', *options, prompt]
    return ['codex', 'exec', 'resume', *options, session, prompt]


def parse_events(lines: list[str]) -> tuple[str | None, str | None]:
    """Return the session ID and the last agent message from the CLI's JSON event lines."""
    session = report = None
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get('type') == 'thread.started':
            session = event.get('thread_id')
        item = event.get('item') or {}
        if item.get('type') == 'agent_message':
            report = item.get('text')
    return session, report


def main(argv: list[str] | None = None) -> int:
    """Dispatch the reviewer and print its session ID and report.

    Args:
        argv: CLI arguments, defaulting to the process arguments.

    Returns:
        The CLI's exit status, or one if it succeeded without returning a report.
    """
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--target', type=Path, default=Path.cwd(), help='the repository or frozen worktree')
    parser.add_argument('--record', required=True, help='the working record holding the evidence')
    parser.add_argument('--log', type=Path, required=True, help="where to write the CLI's raw events")
    parser.add_argument('--boundary', help='the accepted boundary; required for a first assessment')
    parser.add_argument('--purpose', help='the purpose in a few sentences; required for a first assessment')
    parser.add_argument('--scope', default='every changed tracked file', help='the in-scope paths')
    parser.add_argument('--ignore', action='append', default=[], help='an unrelated dirty path; repeatable')
    parser.add_argument('--resume', metavar='SESSION', help='continue this recorded reviewer')
    parser.add_argument('--delta', help='what changed; required with --resume')
    args = parser.parse_args(argv)
    target = args.target.resolve()
    if args.resume:
        if not args.delta:
            parser.error('--resume needs --delta')
        prompt = follow_up_prompt(target, args.record, args.delta)
    else:
        if not args.boundary or not args.purpose:
            parser.error('a first assessment needs --boundary and --purpose')
        prompt = first_prompt(target, args.boundary, args.record, args.purpose, args.scope, args.ignore)
    with args.log.open('w', encoding='utf-8') as log:
        status = subprocess.run(
            codex_command(prompt, args.resume), cwd=target, stdin=subprocess.DEVNULL, stdout=log,
        ).returncode
    session, report = parse_events(args.log.read_text(encoding='utf-8').splitlines())
    print(f'SESSION {session}')
    print(report)
    if status == 0 and report is None:
        print('dispatch: the reviewer returned no report', file=sys.stderr)
        return 1
    return status


if __name__ == '__main__':
    sys.exit(main())
