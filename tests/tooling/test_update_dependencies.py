"""Exercise the dependency-update script with an owned uv stub, never a real uv or network."""

import shlex
import shutil
from pathlib import Path

import pytest

# Shell scripts are not importable, so the script under test is named by path instead of `testee`.
SCRIPT = Path(__file__).resolve().parents[2] / 'update-dependencies.sh'


def run_update(runner, *args, lock_status=0):
    """Run a copy of the script, from its own owned directory, with a ``uv`` stub first on ``PATH``.

    The script changes into its own directory, so it runs as a copy under ``runner.cwd / 'repo'``
    rather than in place: neither the script nor a mutant of it can reach the real repository. The
    stub records its working directory and arguments, one invocation per line, and exits with
    ``lock_status`` for ``uv lock`` and 0 otherwise.

    Args:
        runner: The shared shell-script runner; its working directory holds the copy, stub and log.
        *args: Arguments passed to the script.
        lock_status: Exit status of the stubbed ``uv lock``.

    Returns:
        The completed process, the copied repository directory, and the recorded invocations.
    """
    repo = runner.cwd / 'repo'
    repo.mkdir()
    script = repo / SCRIPT.name
    shutil.copy2(SCRIPT, script)
    stub_dir = runner.cwd / 'bin'
    stub_dir.mkdir()
    log = runner.cwd / 'uv-invocations'
    stub = stub_dir / 'uv'
    stub.write_text(
        '#!/bin/bash\n'
        f'echo "$PWD|$*" >> {shlex.quote(str(log))}\n'
        f'if [ "$1" = lock ]; then exit {lock_status}; fi\n',
        encoding='utf-8',
    )
    stub.chmod(0o755)
    result = runner(script, *args, path_dirs=(stub_dir,))
    recorded = log.read_text(encoding='utf-8').splitlines() if log.exists() else []
    return result, repo, recorded


def test_no_packages_upgrades_every_package_then_syncs_locked(run_shell_script):
    """Without arguments, uv upgrades the whole lock and then syncs, both from the script's own directory."""
    result, repo, recorded = run_update(run_shell_script)

    assert result.returncode == 0, result.stderr
    assert recorded == [f'{repo}|lock --upgrade', f'{repo}|sync --locked']


def test_named_packages_upgrade_only_those_then_sync(run_shell_script):
    """Each named package becomes one --upgrade-package option, and nothing else is upgraded."""
    result, repo, recorded = run_update(run_shell_script, 'yt-dlp', 'pathlib-extensions')

    assert result.returncode == 0, result.stderr
    assert recorded == [
        f'{repo}|lock --upgrade-package yt-dlp --upgrade-package pathlib-extensions',
        f'{repo}|sync --locked',
    ]


def test_failed_lock_stops_before_sync_with_its_status(run_shell_script):
    """A failing uv lock ends the script with its status, leaving the venv unsynced."""
    result, repo, recorded = run_update(run_shell_script, lock_status=3)

    assert result.returncode == 3
    assert recorded == [f'{repo}|lock --upgrade']


@pytest.mark.parametrize('option', ['--frozen', '-U'])
def test_option_argument_is_rejected_before_uv_runs(run_shell_script, option):
    """An option among the package names fails with a message instead of reaching uv."""
    result, _, recorded = run_update(run_shell_script, 'yt-dlp', option)

    assert result.returncode == 2
    assert recorded == []
    assert result.stderr == f"update-dependencies: expected package names, got option '{option}'\n"
