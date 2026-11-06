#!/usr/bin/env python3
"""Capture a frozen Git candidate without traversing working-tree symlinks.

Run from any repository subdirectory; arguments name root-relative, non-ignored
untracked files. Write JSON snapshots outside the repository and compare only
successful runs. A submodule, or a tracked path below a working-tree symlink,
makes the snapshot fail, because Git would scan outside the root index. This is an integrity check, not
protection against concurrent filesystem mutation. Git must be installed; no
application imports are needed.
"""

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path


def git(root: Path, *arguments: str) -> bytes:
    """Read Git output, propagating failures without emitting partial snapshots.

    Optional locks are disabled so that `git status` does not rewrite the index
    to refresh stale stat data; snapshotting must not write to the repository.
    Callers must likewise avoid commands that refresh the index regardless of
    that option, such as `git diff`, or that read tracked paths through
    working-tree symlinks, such as `git ls-files --modified`.

    Args:
        root: Directory from which Git resolves the repository.
        *arguments: Literal Git command arguments.

    Returns:
        Raw output so NUL-separated paths retain arbitrary filename bytes.
    """
    return subprocess.run(
        ['git', '--no-optional-locks', '--literal-pathspecs', *arguments], cwd=root,
        check=True, capture_output=True,
    ).stdout


def path_records(output: bytes) -> list[str]:
    """Decode NUL-separated Git records without stripping filename whitespace."""
    return [os.fsdecode(record) for record in output.split(b'\0') if record]


def symlinked_ancestors(root: Path, names: list[str]) -> list[str]:
    """Find working-tree symlinks that replace parent directories of tracked paths.

    Git working-tree scans such as `git status` stat tracked paths through such a
    link, reaching outside the repository. Only repository components are
    examined, each with `lstat`, and a walk stops at the first symlink, missing
    path or non-directory, so no link is followed.

    Args:
        root: Repository root in a frozen working tree.
        names: Root-relative tracked paths read from the index.

    Returns:
        Sorted root-relative symlink paths; empty when no tracked path is affected.
    """
    links: set[str] = set()
    walkable: dict[Path, bool] = {}
    for name in names:
        parent = Path()
        for part in Path(name).parts[:-1]:
            parent /= part
            if parent not in walkable:
                try:
                    mode = (root / parent).lstat().st_mode
                except (FileNotFoundError, NotADirectoryError):
                    mode = 0
                if stat.S_ISLNK(mode):
                    links.add(parent.as_posix())
                walkable[parent] = stat.S_ISDIR(mode)
            if not walkable[parent]:
                break
    return sorted(links)


def working_entry(root: Path, name: str) -> dict[str, str | int]:
    """Describe a working path, recording symlinks before accessing descendants.

    Args:
        root: Repository root in a frozen working tree.
        name: Canonical root-relative path supplied by Git.

    Returns:
        Path, kind and applicable metadata; absent and special files are not read.
    """
    path = root
    parts = Path(name).parts
    for index, part in enumerate(parts):
        path = path / part
        try:
            mode = path.lstat().st_mode
        except (FileNotFoundError, NotADirectoryError):
            return {'path': name, 'kind': 'absent'}
        if stat.S_ISLNK(mode):
            return {
                'path': name,
                'kind': 'symlink' if index == len(parts) - 1 else 'symlink-ancestor',
                'link': path.relative_to(root).as_posix(),
                'target': os.readlink(path),
            }
    entry: dict[str, str | int] = {'path': name, 'mode': stat.S_IMODE(mode)}
    if stat.S_ISREG(mode):
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        entry.update(kind='file', sha256=digest.hexdigest())
    else:
        entry.update(kind='special', file_type=stat.S_IFMT(mode))
    return entry


def collect_snapshot(directory: Path, untracked: list[str]) -> dict[str, object]:
    """Collect HEAD, status, index and selected working-tree fingerprints.

    Args:
        directory: Any directory within the repository.
        untracked: Exact root-relative untracked file names to include.

    Returns:
        JSON-compatible records, with index object IDs and modes independent of
        working-tree content. Unselected untracked paths appear only in status.

    Raises:
        ValueError: The index contains a submodule, a tracked path lies below a
            working-tree symlink, or an argument is not an existing, non-ignored
            untracked file.
        OSError: A filesystem operation fails.
        subprocess.CalledProcessError: Git cannot inspect the repository.
    """
    root = Path(os.fsdecode(git(directory, 'rev-parse', '--show-toplevel')).removesuffix('\n'))
    # Refuse before any Git command scans the working tree; ls-files --stage reads only the index.
    index = path_records(git(root, 'ls-files', '--stage', '-z'))
    names = [record.split('\t', 1)[1] for record in index]
    # The root index does not describe a submodule's contents, which `git status` scans recursively.
    gitlinks = [name for record, name in zip(index, names) if record.startswith('160000 ')]
    if gitlinks:
        raise ValueError(f'submodules are unsupported, since Git would scan their working trees: {gitlinks!r}')
    links = symlinked_ancestors(root, names)
    if links:
        raise ValueError(f'tracked paths lie below working-tree symlinks, which Git would traverse: {links!r}')
    # In a frozen tree Git lists only eligible files/links, not directories or FIFOs.
    allowed = set(path_records(git(root, 'ls-files', '-z', '--others', '--exclude-standard')))
    for name in untracked:
        if name not in allowed:
            raise ValueError(f'not an in-scope untracked file: {name!r}')
    staged = path_records(git(root, 'diff', '-z', '--name-only', '--no-renames', '--cached', 'HEAD'))
    # Unstaged paths come from the status worktree column: `git status` treats a tracked path below a
    # working-tree symlink as deleted, whereas `ls-files --modified` reads through the link, and `git diff`
    # rewrites a stale index. --no-renames keeps every -z record a single "XY path" entry.
    status = path_records(git(root, 'status', '--porcelain=v1', '-z', '--untracked-files=all', '--no-renames'))
    unstaged = [record[3:] for record in status if record[1] not in ' ?']
    return {
        'head': os.fsdecode(git(root, 'rev-parse', 'HEAD')).removesuffix('\n'),
        'status': status,
        'index': index,
        'entries': [working_entry(root, name) for name in sorted(set(staged + unstaged + untracked))],
    }


def main(argv: list[str] | None = None) -> int:
    """Print a complete JSON snapshot, or diagnose failure on stderr with exit 2.

    Args:
        argv: CLI arguments, defaulting to the process arguments.

    Returns:
        Zero on success, or two if validation, Git or filesystem access fails.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('untracked', nargs='*', help='root-relative in-scope untracked files')
    args = parser.parse_args(argv)
    try:
        snapshot = collect_snapshot(Path.cwd(), args.untracked)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f'snapshot: {error}', file=sys.stderr)
        return 2
    print(json.dumps(snapshot, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
