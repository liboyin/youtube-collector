#!/usr/bin/env python3
"""Freeze a candidate in its own git worktree and run the required gates there.

The worktree starts at ``HEAD`` and receives only the named files; a named file missing from the
repository is removed from the worktree, so deletions are part of the candidate. The gates run with
``PYTHONPATH`` set to the worktree, and ``check-coverage.sh`` refuses to measure any other package.
The worktree is left in place as the review target.

Two further modes serve the commit. ``--compare``, before the commit, checks that the repository's
working files still match the reviewed ones. ``--remove``, after the commit, removes the worktree,
and refuses unless the commit at the repository's ``HEAD`` holds the reviewed content. The
candidate's files are uncommitted in the worktree by design, so plain ``git worktree remove``
refuses; ``--remove`` forces the removal only after that check. ``--changed PATH`` names a reviewed
file that is expected to differ, such as a task record that gained its results after the review;
both modes skip it.

Freezing records the worktree's ``HEAD`` and each named path with its fingerprint in the worktree's own
git directory. Both modes refuse a worktree whose ``HEAD`` moved, whose named files no longer match that
record, or that changed a path outside them, and ``--remove`` also refuses a worktree whose index
stages anything, so a change made in the worktree after the freeze is never compared away or
discarded unseen.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

GATES = (('./check-coverage.sh',), ('mypy', 'youtube_collector'), ('ruff', 'check', '.'))
MANIFEST = 'worktree-gates.json'


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
    record = {
        'head': _git(worktree, 'rev-parse', 'HEAD').strip(),
        'files': {name: _recorded(worktree / name) for name in files},
    }
    _manifest_path(worktree).write_text(json.dumps(record, indent=1, sort_keys=True), encoding='utf-8')


def _git(root: Path, *arguments: str, env: dict[str, str] | None = None) -> str:
    """Return the output of a git command run in ``root``; pathspecs are taken literally."""
    command = ['git', '--literal-pathspecs', '-C', str(root), *arguments]
    return subprocess.run(command, check=True, capture_output=True, text=True, env=env).stdout


def reviewed_content(worktree: Path) -> tuple[str, list[str]]:
    """Return a git tree of the worktree's files and the paths in which it differs from its ``HEAD``.

    The tree is built in a temporary index, so the worktree's own index does not change. Ignored
    files, such as the caches the gates leave, are not part of it. File modes are always read from
    the files: with ``core.filemode`` off, git would drop a mode change, and the removal would then
    destroy it unseen.
    """
    with tempfile.TemporaryDirectory(prefix='worktree-gates-') as temporary:
        env = {**os.environ, 'GIT_INDEX_FILE': str(Path(temporary) / 'index')}
        _git(worktree, 'read-tree', 'HEAD', env=env)
        _git(worktree, '-c', 'core.filemode=true', 'add', '-A', env=env)
        tree = _git(worktree, 'write-tree', env=env).strip()
    names = _git(worktree, 'diff', '--name-only', '--no-renames', '-z', 'HEAD', tree)
    return tree, [name for name in names.split('\0') if name]


def _fingerprint(path: Path) -> tuple[str, bytes] | None:
    """Return what identifies a working file: its kind, with a link's target or a file's content and mode."""
    if path.is_symlink():
        return 'link', os.fsencode(path.readlink())
    if not path.exists():
        return None
    return f'file {os.access(path, os.X_OK)}', path.read_bytes()


def _recorded(path: Path) -> list[str] | None:
    """Return a path's fingerprint in the form the freeze record stores: its kind and a SHA-256."""
    fingerprint = _fingerprint(path)
    return None if fingerprint is None else [fingerprint[0], hashlib.sha256(fingerprint[1]).hexdigest()]


def _manifest_path(worktree: Path) -> Path:
    """Return where the freeze record of a worktree lives: its own git directory, outside its files."""
    return Path(_git(worktree, 'rev-parse', '--absolute-git-dir').strip()) / MANIFEST


def reviewed_names(worktree: Path, changed: frozenset[str]) -> tuple[str, list[str]]:
    """Return the reviewed tree and the paths named at the freeze, less ``changed``, after checking the worktree.

    Raises:
        ValueError: If the worktree has no readable freeze record, its ``HEAD`` moved, a named path lies below a
            symlink in it, a named file no longer matches its recorded fingerprint, or a path that was
            not named differs from ``HEAD``.
    """
    path = _manifest_path(worktree)
    if not path.is_file():
        raise ValueError(f'{worktree} has no freeze record; only a worktree that this tool froze can be used')
    record = json.loads(path.read_text(encoding='utf-8'))
    valid = isinstance(record, dict) and isinstance(record.get('head'), str) and isinstance(record.get('files'), dict)
    if not valid:
        # A ValueError, not a TypeError: the record is data from disk, and main reports a ValueError as a refusal.
        raise ValueError(f'{path} is not a freeze record of this tool')
    if _git(worktree, 'rev-parse', 'HEAD').strip() != record['head']:
        raise ValueError(f"the frozen worktree's HEAD moved from {record['head']} after the freeze")
    files = record['files']
    names = sorted(files)
    _refuse_symlinked_parents(worktree, names)
    tree, differing = reviewed_content(worktree)
    drifted = [name for name in names if _recorded(worktree / name) != files[name]]
    drifted += sorted(set(differing) - set(files))
    if drifted:
        raise ValueError(f'the frozen worktree changed after the freeze in {drifted}')
    return tree, [name for name in names if name not in changed]


def compare(repo: Path, worktree: Path, changed: frozenset[str]) -> list[str]:
    """Return the reviewed paths whose working files in the repository differ from the worktree's.

    Paths in ``changed`` are expected to differ and are skipped.

    Raises:
        ValueError: If the worktree no longer matches its freeze record, or a reviewed path lies below
            a symlink in either tree.
    """
    names = reviewed_names(worktree, changed)[1]
    _refuse_symlinked_parents(repo, names)
    _refuse_symlinked_parents(worktree, names)
    return [name for name in names if _fingerprint(repo / name) != _fingerprint(worktree / name)]


def remove(repo: Path, worktree: Path, changed: frozenset[str]) -> list[str]:
    """Remove the worktree if the commit at the repository's ``HEAD`` holds the reviewed content.

    Paths in ``changed`` are expected to differ and are skipped; the worktree's version of them is lost.

    Returns:
        The reviewed paths that the commit does not hold; the worktree is removed only if there are none.

    Raises:
        ValueError: If the worktree no longer matches its freeze record, or its index stages a change,
            which the forced removal would discard.
    """
    tree, names = reviewed_names(worktree, changed)
    index = _git(worktree, 'diff', '--cached', '--name-only', '--no-renames', '-z', 'HEAD')
    staged = [name for name in index.split('\0') if name]
    if staged:
        raise ValueError(f'the worktree index stages changes in {staged}; the removal would discard them')
    missing = _git(repo, 'diff', '--name-only', '--no-renames', '-z', tree, 'HEAD', '--', *names) if names else ''
    uncommitted = [name for name in missing.split('\0') if name]
    if not uncommitted:
        _git(repo, 'worktree', 'remove', '--force', str(worktree))
    return uncommitted


def run_gates(worktree: Path) -> bool:
    """Run every gate in the worktree, even after one fails; return whether all passed."""
    env = {**os.environ, 'PYTHONPATH': str(worktree)}
    passed = True
    for gate in GATES:
        status = subprocess.run(gate, cwd=worktree, env=env, stdin=subprocess.DEVNULL, check=False).returncode
        print(f"{'PASSED' if status == 0 else 'FAILED'} {' '.join(gate)}", flush=True)
        passed = passed and status == 0
    return passed


def main(argv: list[str] | None = None) -> int:
    """Freeze the candidate and run the gates, or compare or remove an existing worktree.

    Args:
        argv: CLI arguments, defaulting to the process arguments.

    Returns:
        Zero when every gate passed, the files match or the worktree was removed. One when a gate
        failed, a file differs or the removal was refused. Two when the command could not run.
    """
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('worktree', type=Path, help='a new path, outside the repository, or the existing worktree')
    parser.add_argument('files', nargs='*', help="the candidate's root-relative files, to freeze it")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        '--compare', action='store_true', help="check that the repository's working files match the reviewed ones",
    )
    modes.add_argument(
        '--remove', action='store_true', help='remove the worktree if the commit at HEAD holds the reviewed content',
    )
    parser.add_argument(
        '--changed', action='append', default=[], metavar='PATH',
        help='a reviewed file that is expected to differ; --compare and --remove skip it; repeatable',
    )
    parser.add_argument('--repo', type=Path, default=Path.cwd(), help='the repository; default: the working directory')
    args = parser.parse_args(argv)
    if bool(args.files) == (args.compare or args.remove):
        parser.error('name the files to freeze a candidate, and no files with --compare or --remove')
    repo, worktree = args.repo.resolve(), args.worktree.resolve()
    try:
        if args.compare or args.remove:
            changed = frozenset(args.changed)
            differing = compare(repo, worktree, changed) if args.compare else remove(repo, worktree, changed)
            if differing:
                where = "the repository's working files" if args.compare else 'the commit at HEAD'
                kept = '' if args.compare else '; the worktree is kept'
                print(f'worktree_gates: {where} differ from the reviewed content of {differing}{kept}', file=sys.stderr)
            return 1 if differing else 0
        freeze(repo, worktree, args.files)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f'worktree_gates: {error}', file=sys.stderr)
        return 2
    return 0 if run_gates(worktree) else 1


if __name__ == '__main__':
    sys.exit(main())
