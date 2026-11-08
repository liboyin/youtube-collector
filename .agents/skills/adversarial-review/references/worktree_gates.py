#!/usr/bin/env python3
"""Freeze a candidate in its own git worktree and run the required gates there.

The worktree starts at ``HEAD`` and receives only the named files; a named file missing from the
repository is removed from the worktree, so deletions are part of the candidate. The gates run with
``PYTHONPATH`` set to the worktree, and ``check-coverage.sh`` refuses to measure any other package.
The worktree is left in place as the review target; remove it with ``git worktree remove``.
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

GATES = (('./check-coverage.sh',), ('mypy', 'youtube_collector'), ('ruff', 'check', '.'))


def _refuse_symlinked_parents(root: Path, files: list[str]) -> None:
    """Raise ValueError if a file lies below a symlink under ``root``."""
    for name in files:
        if any((root / parent).is_symlink() for parent in PurePosixPath(name).parents):
            raise ValueError(f'{name} lies below a symlink in {root}')


def freeze(repo: Path, worktree: Path, files: list[str]) -> None:
    """Create a detached worktree at ``HEAD`` and apply the candidate's files to it.

    Paths are checked before anything is written or removed, in the repository and then in the new
    worktree, whose tracked symlinks could otherwise redirect a copy or a removal outside it.

    Raises:
        ValueError: If a file is not a relative path inside the repository or lies below a symlink.
        subprocess.CalledProcessError: If git cannot create the worktree.
    """
    for name in files:
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts:
            raise ValueError(f'{name} is not a relative path inside the repository')
    _refuse_symlinked_parents(repo, files)
    subprocess.run(
        ['git', '-C', str(repo), 'worktree', 'add', '--detach', str(worktree), 'HEAD'], check=True, capture_output=True,
    )
    try:
        _refuse_symlinked_parents(worktree, files)
    except ValueError:
        subprocess.run(
            ['git', '-C', str(repo), 'worktree', 'remove', '--force', str(worktree)], check=True, capture_output=True,
        )
        raise
    for name in files:
        source, target = repo / name, worktree / name
        if source.is_symlink() or source.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.unlink(missing_ok=True)
            shutil.copy2(source, target, follow_symlinks=False)
        else:
            target.unlink(missing_ok=True)


def run_gates(worktree: Path) -> bool:
    """Run every gate in the worktree, even after one fails; return whether all passed."""
    env = {**os.environ, 'PYTHONPATH': str(worktree)}
    passed = True
    for gate in GATES:
        status = subprocess.run(gate, cwd=worktree, env=env, stdin=subprocess.DEVNULL).returncode
        print(f"{'PASSED' if status == 0 else 'FAILED'} {' '.join(gate)}", flush=True)
        passed = passed and status == 0
    return passed


def main(argv: list[str] | None = None) -> int:
    """Freeze the candidate and run the gates.

    Args:
        argv: CLI arguments, defaulting to the process arguments.

    Returns:
        Zero when every gate passed, one when any failed, two when the worktree could not be made.
    """
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('worktree', type=Path, help='a new path, outside the repository')
    parser.add_argument('files', nargs='+', help="the candidate's root-relative files")
    parser.add_argument('--repo', type=Path, default=Path.cwd(), help='the repository; default: the working directory')
    args = parser.parse_args(argv)
    worktree = args.worktree.resolve()
    try:
        freeze(args.repo.resolve(), worktree, args.files)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f'worktree_gates: {error}', file=sys.stderr)
        return 2
    return 0 if run_gates(worktree) else 1


if __name__ == '__main__':
    sys.exit(main())
