#!/usr/bin/env python3
"""Run named mutants against an isolated scratch copy of the repository's candidate.

A mutants file is a Python file defining ``MUTANTS``, a list of tuples:
``(name, invariant, root-relative path, old text, new text, [pytest arguments])``. Each mutant's
pytest arguments are the narrowest tests that observe its invariant, run in a fresh process with
every path, the home directory and the temporary directory inside the copy. The copy holds the
tracked files and only the untracked files named with ``--untracked``. A plugin outside the copy
checks that application modules came from the copy and none from the original repository, records
why each test failed and lists live threads.

A mutant counts as killed only if tests failed through their assertions rather than through broken
code, none errored, and provenance was verified.
"""

import argparse
import hashlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_PLUGIN_NAME = 'review_provenance'
_PROVENANCE_MARK = 'ACTUAL_IMPORT_PROVENANCE'
_DETAIL_MARK = 'FAILURE_DETAIL '
# Fixed so that a run can be reproduced from its evidence.
_RANDOM_SEED = 20261108
_PLUGIN = '''"""Verify that no module came from the original repository, and record why each test failed."""
import os
import sys
import threading
from pathlib import Path

import pytest

_DETAILS = []


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Record each test failure, marking broken code as non-behavioral."""
    outcome = yield
    report = outcome.get_result()
    if report.failed and call.excinfo is not None:
        broken = isinstance(call.excinfo.value, (SyntaxError, ImportError, NameError))
        kind = 'NON_BEHAVIORAL' if broken else 'BEHAVIORAL'
        _DETAILS.append(f'{kind} {item.nodeid}: {call.excinfo.exconly()[:300]!r}')


def pytest_sessionfinish(session, exitstatus):
    """Fail the run if a module escaped the copy; write this run's own evidence file otherwise.

    A file, not stdout: a nested pytest's output can appear in a failure report after this one's.
    """
    root = Path(os.environ['MUTATE_SCRATCH']).resolve()
    repo = Path(os.environ['MUTATE_REPO']).resolve()
    modules = list(sys.modules.items())
    files = {name: Path(module.__file__).resolve() for name, module in modules if getattr(module, '__file__', None)}
    application = {name: file for name, file in files.items() if name.split('.')[0] == 'youtube_collector'}
    escaped = [str(file) for file in files.values() if file.is_relative_to(repo)]
    escaped += [str(file) for file in application.values() if not file.is_relative_to(root)]
    if escaped:
        raise pytest.UsageError(f'Import escaped scratch: {escaped}')
    alive = [thread.name for thread in threading.enumerate() if thread is not threading.main_thread()]
    copied = sum(file.is_relative_to(root) for file in files.values())
    lines = [*(f'FAILURE_DETAIL {detail}' for detail in _DETAILS)]
    lines.append(f'ACTUAL_IMPORT_PROVENANCE scratch={root} modules={copied} threads={alive}')
    Path(os.environ['MUTATE_REPORT']).write_text('\\n'.join(lines) + '\\n', encoding='utf-8')
'''


@dataclass(frozen=True)
class Mutant:
    """One mutation and the tests that observe it."""

    name: str
    invariant: str
    path: str
    old: str
    new: str
    tests: list[str]


@dataclass(frozen=True)
class Run:
    """What one pytest process reported; ``exit_code`` is None when it timed out."""

    exit_code: int | None
    summary: str
    provenance: str | None
    failed: tuple[str, ...]
    details: tuple[str, ...] = ()


def _listed(repo: Path, *options: str) -> set[str]:
    """Return the paths ``git ls-files`` lists with the given options."""
    output = subprocess.run(['git', 'ls-files', '-z', *options], cwd=repo, check=True, capture_output=True).stdout
    return {raw.decode() for raw in output.split(b'\0') if raw}


def candidate_files(repo: Path, untracked: frozenset[str]) -> list[str]:
    """Return the tracked paths that exist, plus the explicitly named untracked ones.

    Raises:
        ValueError: If a named path is ignored or absent, which must never be copied.
    """
    tracked = _listed(repo, '--cached')
    unknown = untracked - tracked - _listed(repo, '--others', '--exclude-standard')
    if unknown:
        raise ValueError(f'Not untracked, non-ignored files: {sorted(unknown)}')
    return [name for name in sorted(tracked | untracked) if (repo / name).is_symlink() or (repo / name).is_file()]


def copy_candidate(repo: Path, tree: Path, names: list[str]) -> None:
    """Copy the named files into ``tree``, keeping modes and recreating repository-relative symlinks.

    ``follow_symlinks=False`` protects only the final path component, so symlinked parents are refused.

    Raises:
        ValueError: If a path lies below a symlink, or a symlink is absolute or leaves the repository;
            the copy could not contain either.
    """
    for name in names:
        if any((repo / parent).is_symlink() for parent in PurePosixPath(name).parents):
            raise ValueError(f'{name} lies below a symlink')
        source = repo / name
        if source.is_symlink() and (source.readlink().is_absolute() or not source.resolve().is_relative_to(repo)):
            raise ValueError(f'{name} is a symlink out of the repository')
        target = tree / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target, follow_symlinks=False)


def candidate_digest(tree: Path, names: list[str]) -> str:
    """Return a SHA-256 of the copy's paths and contents, which identifies the tested candidate."""
    digest = hashlib.sha256()
    for name in names:
        path = tree / name
        content = os.fsencode(path.readlink()) if path.is_symlink() else path.read_bytes()
        digest.update(name.encode() + b'\0' + content)
    return digest.hexdigest()


def load_mutants(path: Path) -> list[Mutant]:
    """Load ``MUTANTS`` from a mutants file.

    Raises:
        ValueError: If the file cannot be loaded as a module.
    """
    spec = importlib.util.spec_from_file_location('review_mutants', path)
    if spec is None or spec.loader is None:
        raise ValueError(f'Cannot load mutants from {path}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return [Mutant(*entry) for entry in module.MUTANTS]


def run_tests(tree: Path, owned: Path, plugin: Path, repo: Path, tests: list[str], timeout: float) -> Run:
    """Run pytest in a fresh process whose environment is built from scratch rather than inherited."""
    report = owned / 'report.txt'
    report.unlink(missing_ok=True)
    env = {
        'PATH': os.environ['PATH'], 'HOME': str(owned), 'TMPDIR': str(owned), 'LC_ALL': 'C.UTF-8',
        'PYTHONPATH': os.pathsep.join((str(tree), str(plugin))), 'PYTHONDONTWRITEBYTECODE': '1',
        'MUTATE_SCRATCH': str(tree), 'MUTATE_REPO': str(repo), 'MUTATE_REPORT': str(report),
    }
    command = [
        sys.executable, '-m', 'pytest', *tests, '--no-cov', '-q', '-p', 'no:cacheprovider', '-p', _PLUGIN_NAME,
        f'--randomly-seed={_RANDOM_SEED}', f'--basetemp={owned / "pytest"}',
    ]
    try:
        done = subprocess.run(
            command, cwd=tree, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return Run(None, f'timed out after {timeout:g} s', None, ())
    lines = (done.stdout + done.stderr).splitlines()
    outcomes = [line for line in lines if re.search(r'\d+ (passed|failed|errors?)\b', line)]
    evidence = report.read_text(encoding='utf-8').splitlines() if report.exists() else []
    marks = [line for line in evidence if line.startswith(_PROVENANCE_MARK)]
    return Run(
        done.returncode, outcomes[-1] if outcomes else '\n'.join(lines[-3:]), marks[-1] if marks else None,
        tuple(line for line in lines if line.startswith(('FAILED', 'ERROR'))),
        tuple(line[len(_DETAIL_MARK):] for line in evidence if line.startswith(_DETAIL_MARK)),
    )


def verdict(run: Run) -> str:
    """Return KILLED, SURVIVED or INVALID; a run without verified provenance is never evidence."""
    if run.provenance is None:
        return 'INVALID'
    if run.exit_code == 0:
        return 'SURVIVED'
    errored = any(line.startswith('ERROR') for line in run.failed)
    broken = any(line.startswith('NON_BEHAVIORAL') for line in run.details)
    return 'KILLED' if run.exit_code == 1 and run.failed and run.details and not errored and not broken else 'INVALID'


def run_mutants(mutants: list[Mutant], repo: Path, untracked: frozenset[str], evidence: Path, timeout: float) -> int:
    """Run every mutant in one owned copy, restoring it after each; return how many were not killed.

    Evidence is appended as each mutant finishes, so an interrupted batch keeps what it produced.

    Raises:
        ValueError: If a mutant's old text does not occur exactly once, or the baseline fails.
    """
    with tempfile.TemporaryDirectory(prefix='mutate-') as owned_root:
        root = Path(owned_root).resolve()
        tree, owned, plugin = root / 'tree', root / 'owned', root / 'plugin'
        for directory in (tree, owned, plugin):
            directory.mkdir()
        (plugin / f'{_PLUGIN_NAME}.py').write_text(_PLUGIN, encoding='utf-8')
        names = candidate_files(repo, untracked)
        copy_candidate(repo, tree, names)
        for mutant in mutants:
            count = (tree / mutant.path).read_text(encoding='utf-8').count(mutant.old)
            if count != 1:
                raise ValueError(f'{mutant.name}: the old text occurs {count} times in {mutant.path}')
        selected = sorted({test for mutant in mutants for test in mutant.tests})
        baseline = run_tests(tree, owned, plugin, repo, selected, timeout)
        if baseline.exit_code != 0 or baseline.provenance is None:
            raise ValueError(f'Baseline did not pass cleanly: {baseline.summary}; {baseline.failed}')
        evidence.write_text(
            f'CANDIDATE sha256={candidate_digest(tree, names)} files={len(names)} randomly-seed={_RANDOM_SEED}\n'
            f'BASELINE {selected}: {baseline.summary}\n  {baseline.provenance}\n',
            encoding='utf-8',
        )
        not_killed = 0
        for mutant in mutants:
            file = tree / mutant.path
            original = file.read_text(encoding='utf-8')
            file.write_text(original.replace(mutant.old, mutant.new), encoding='utf-8')
            try:
                run = run_tests(tree, owned, plugin, repo, mutant.tests, timeout)
            finally:
                file.write_text(original, encoding='utf-8')
            result = verdict(run)
            not_killed += result != 'KILLED'
            report = '\n  '.join((
                f'{mutant.name} [{result}] {mutant.invariant}',
                f'mutant: {mutant.path}: {mutant.old!r} -> {mutant.new!r}',
                f'tests: {mutant.tests} exit={run.exit_code} {run.summary}', f'{run.provenance}', *run.failed[:8],
                *run.details[:8],
            ))
            with evidence.open('a', encoding='utf-8') as handle:
                handle.write(report + '\n')
            print(report, flush=True)
    return not_killed


def main(argv: list[str] | None = None) -> int:
    """Run a mutants file.

    Args:
        argv: CLI arguments, defaulting to the process arguments.

    Returns:
        Zero when every mutant was killed, one when any was not, two when the batch could not run.
    """
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('mutants', type=Path, help='the mutants file')
    parser.add_argument('--evidence', type=Path, required=True, help='where to write the evidence')
    parser.add_argument('--repo', type=Path, default=Path.cwd(), help='the repository; default: the working directory')
    parser.add_argument('--untracked', action='append', default=[], help='an in-scope untracked path; repeatable')
    parser.add_argument('--timeout', type=float, default=280.0, help='seconds allowed for each pytest process')
    args = parser.parse_args(argv)
    try:
        not_killed = run_mutants(
            load_mutants(args.mutants), args.repo.resolve(), frozenset(args.untracked), args.evidence, args.timeout,
        )
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f'mutate: {error}', file=sys.stderr)
        return 2
    print(f'Not killed: {not_killed}')
    return 1 if not_killed else 0


if __name__ == '__main__':
    sys.exit(main())
