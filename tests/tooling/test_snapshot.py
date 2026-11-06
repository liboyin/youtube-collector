"""Exercise review tooling with local Git repositories and test-owned data."""

import builtins
import hashlib
import importlib.util
import io
import json
import os
import runpy
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

# AGENTS permits file-based loading for standalone skill scripts outside the package.
SCRIPT = Path(__file__).resolve().parents[2] / '.agents/skills/adversarial-review/references/snapshot.py'
SPEC = importlib.util.spec_from_file_location('review_snapshot', SCRIPT)
assert SPEC is not None and SPEC.loader is not None
testee = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(testee)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """Own Git configuration, environment, files and command execution per test."""
    for name in tuple(os.environ):
        if name.startswith('GIT_'):
            monkeypatch.delenv(name)
    # Git only honors this boundary when discovery starts below it.
    monkeypatch.setenv('GIT_CEILING_DIRECTORIES', str(tmp_path))
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(home))
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', os.devnull)
    monkeypatch.setenv('GIT_TEMPLATE_DIR', str(home))
    root = tmp_path / 'repo'
    root.mkdir()
    monkeypatch.chdir(root)
    def git(*args):
        """Run Git only in this test's repository with isolated configuration."""
        return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True)

    git('init', '-q')
    git('config', 'user.name', 'Snapshot test')
    git('config', 'user.email', 'test@example.invalid')
    git('config', 'core.filemode', 'true')
    (root / 'file').write_text('base\n')
    (root / 'dir').mkdir()
    (root / 'dir/data').write_text('tracked\n')
    (root / '.gitignore').write_text('ignored\n')
    git('add', 'file', 'dir/data', '.gitignore')
    git('commit', '-qm', 'base')
    return root, git


@contextmanager
def forbid_content_reads():
    """Reject filesystem opens through Path, io or builtins while allowing owned pipe FDs."""
    original_open = io.open

    def guarded_open(file, *args, **kwargs):
        """Allow subprocess pipe wrapping but reject any path-based content access."""
        assert not isinstance(file, (str, bytes, os.PathLike)), 'unexpected content read'
        return original_open(file, *args, **kwargs)

    with patch.object(io, 'open', guarded_open), patch.object(builtins, 'open', guarded_open):
        yield


def test_symlinked_ancestors_reports_only_links_without_walking_past_them(repo, tmp_path):
    """Links replacing tracked parents are reported once; missing or file parents and link descendants are skipped."""
    root, _ = repo
    (tmp_path / 'outside/inner').mkdir(parents=True)
    (root / 'a').mkdir()
    (root / 'a/b').symlink_to(tmp_path / 'outside')
    (root / 'plain').write_text('not a directory')
    names = ['top', 'a/b/one', 'a/b/inner/two', 'missing/x', 'plain/y', 'dir/data']
    with patch.object(testee.Path, 'lstat', autospec=True, side_effect=testee.Path.lstat) as lstat:
        assert testee.symlinked_ancestors(root, names) == ['a/b']
    examined = {path.relative_to(root).as_posix() for (path,), _ in lstat.call_args_list}
    assert examined == {'a', 'a/b', 'missing', 'plain', 'dir'}


def test_working_entry_stops_at_symlinked_ancestor_without_reading(repo, tmp_path):
    """Direct callers also get the link recorded rather than the target behind it."""
    root, _ = repo
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'data').write_text('owned sentinel')
    (root / 'dir/data').unlink()
    (root / 'dir').rmdir()
    (root / 'dir').symlink_to(outside)
    with forbid_content_reads():
        entry = testee.working_entry(root, 'dir/data')
    assert entry == {'path': 'dir/data', 'kind': 'symlink-ancestor', 'link': 'dir', 'target': str(outside)}


def test_working_entry_records_special_files_without_opening_them(repo):
    """A tracked path replaced by a FIFO must not block on content reads."""
    root, _ = repo
    (root / 'file').unlink()
    os.mkfifo(root / 'file')
    assert testee.working_entry(root, 'file')['kind'] == 'special'


def test_collect_snapshot_is_stable_from_subdirectory(repo):
    """Unchanged snapshots agree regardless of invocation directory."""
    root, _ = repo
    assert testee.collect_snapshot(root, []) == testee.collect_snapshot(root / 'dir', [])
    assert testee.collect_snapshot(root, [])['entries'] == []


def test_collect_snapshot_does_not_rewrite_index_with_stale_stat_data(repo):
    """A timestamp-only change is not reported as an edit and leaves the Git index bytes untouched."""
    root, _ = repo
    index = root / '.git/index'
    os.utime(root / 'file', (1_000_000_000, 1_000_000_000))
    before = index.read_bytes()
    assert testee.collect_snapshot(root, [])['entries'] == []
    assert index.read_bytes() == before
    assert not (root / '.git/index.lock').exists()


def test_collect_snapshot_detects_index_changes_with_identical_worktree(repo):
    """Different staged blobs cannot hide behind identical MM status and contents."""
    root, git = repo
    snapshots = []
    for staged in ['first', 'second']:
        (root / 'file').write_text(staged)
        git('add', 'file')
        (root / 'file').write_text('working')
        snapshots.append(testee.collect_snapshot(root, []))
    assert snapshots[0]['status'] == snapshots[1]['status']
    assert snapshots[0]['entries'] == snapshots[1]['entries']
    assert snapshots[0]['index'] != snapshots[1]['index']


def test_collect_snapshot_keeps_cancelled_staged_and_unstaged_changes(repo):
    """Restoring HEAD contents in the worktree does not hide a staged change."""
    root, git = repo
    (root / 'file').write_text('staged')
    git('add', 'file')
    (root / 'file').write_text('base\n')
    assert testee.collect_snapshot(root, [])['entries'][0]['path'] == 'file'


def test_collect_snapshot_records_rename_deletion_and_mode(repo):
    """Rename sides, deletion and executable permissions remain distinguishable."""
    root, git = repo
    git('mv', 'file', 'renamed')
    (root / 'renamed').chmod(0o755)
    (root / 'dir/data').unlink()
    entries = {entry['path']: entry for entry in testee.collect_snapshot(root, [])['entries']}
    assert set(entries) == {'file', 'renamed', 'dir/data'}
    assert entries['file']['kind'] == entries['dir/data']['kind'] == 'absent'
    assert entries['renamed']['mode'] == 0o755
    assert entries['renamed']['sha256'] == hashlib.sha256(b'base\n').hexdigest()


def test_collect_snapshot_never_reads_leaf_symlink_targets(repo, tmp_path):
    """A tracked file replaced by a link preserves the target spelling without reading target data."""
    root, _ = repo
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'data').write_text('owned sentinel')
    (root / 'dir/data').unlink()
    (root / 'dir/data').symlink_to(outside / 'data')
    with forbid_content_reads():
        entry = testee.collect_snapshot(root, [])['entries'][0]
    assert entry['kind'] == 'symlink'
    assert entry['target'] == str(outside / 'data')


def test_collect_snapshot_refuses_symlinked_ancestors_before_scanning_the_worktree(repo, tmp_path):
    """A same-size file behind a symlinked directory fails the snapshot with only index reads performed."""
    root, _ = repo
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'data').write_text('changed\n')  # Same size as the committed 'tracked\n', so Git would compare content.
    (root / 'dir/data').unlink()
    (root / 'dir').rmdir()
    (root / 'dir').symlink_to(outside)
    calls = []
    real_git = testee.git

    def recording_git(directory, *arguments):
        """Record each Git invocation before delegating to the real helper."""
        calls.append(arguments)
        return real_git(directory, *arguments)

    with (
        patch.object(testee, 'git', recording_git),
        forbid_content_reads(),
        pytest.raises(ValueError, match=r"working-tree symlinks.*\['dir'\]"),
    ):
        testee.collect_snapshot(root, [])
    # Neither command touches the working tree; `git status` would stat dir/data through the link.
    assert calls == [('rev-parse', '--show-toplevel'), ('ls-files', '--stage', '-z')]


def test_collect_snapshot_refuses_submodules_before_scanning_the_worktree(repo, tmp_path):
    """A gitlink, here replaced by a link to an owned repository, fails the snapshot with only index reads."""
    root, git = repo
    outside = tmp_path / 'outside'
    outside.mkdir()
    subprocess.run(['git', 'init', '-q'], cwd=outside, check=True)
    git('update-index', '--add', '--cacheinfo', f'160000,{"1" * 40},module')
    (root / 'module').symlink_to(outside)
    calls = []
    real_git = testee.git

    def recording_git(directory, *arguments):
        """Record each Git invocation before delegating to the real helper."""
        calls.append(arguments)
        return real_git(directory, *arguments)

    with (
        patch.object(testee, 'git', recording_git),
        forbid_content_reads(),
        pytest.raises(ValueError, match=r"submodules are unsupported.*\['module'\]"),
    ):
        testee.collect_snapshot(root, [])
    assert calls == [('rev-parse', '--show-toplevel'), ('ls-files', '--stage', '-z')]


def test_collect_snapshot_preserves_unusual_untracked_names_and_link_targets(repo):
    """NUL parsing and JSON preserve tabs, newlines, non-UTF8 bytes and link targets."""
    root, _ = repo
    name = os.fsdecode(b'new\t\n\xff')
    (root / name).write_bytes(b'owned')
    (root / 'link').symlink_to('missing\n')
    (root / 'unselected').write_text('not included')
    snapshot = testee.collect_snapshot(root, [name, 'link', name])
    assert json.loads(json.dumps(snapshot)) == snapshot
    entries = {entry['path']: entry for entry in snapshot['entries']}
    assert set(entries) == {name, 'link'}
    assert entries[name]['sha256'] == hashlib.sha256(b'owned').hexdigest()
    assert entries['link']['target'] == 'missing\n'
    assert '?? unselected' in snapshot['status']


@pytest.mark.parametrize('name', ['file', 'ignored', 'missing', 'dir', '../outside', '/outside', './new', 'pipe'])
def test_collect_snapshot_rejects_noncanonical_or_ineligible_arguments(repo, name):
    """Explicit inputs must be exact non-ignored regular-file or symlink names."""
    root, _ = repo
    (root / 'new').write_text('owned')
    (root / 'ignored').write_text('owned ignored sentinel')
    os.mkfifo(root / 'pipe')
    with (
        forbid_content_reads(),
        pytest.raises(ValueError, match='not an in-scope untracked file'),
    ):
        testee.collect_snapshot(root, [name])


def test_main_prints_parseable_snapshot(repo, capsys):
    """The CLI adapter emits one complete JSON document on success."""
    root, _ = repo
    assert testee.main([]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == testee.collect_snapshot(root, [])
    assert output.err == ''


def test_main_reports_invalid_arguments_without_partial_output(repo, capsys):
    """Invalid inputs exit nonzero without a snapshot that could be mistaken for success."""
    assert testee.main(['ignored']) == 2
    output = capsys.readouterr()
    assert output.out == ''
    assert 'not an in-scope untracked file' in output.err


def test_main_reports_git_failure_without_partial_output(repo, tmp_path, monkeypatch, capsys):
    """Discovery cannot escape the owned ceiling even with a committed outer repository."""
    _, git = repo
    git('-C', str(tmp_path), 'init', '-q')
    git('-C', str(tmp_path), '-c', 'user.name=Snapshot test', '-c',
        'user.email=test@example.invalid', 'commit', '--allow-empty', '-qm', 'owned outer repository')
    outside = tmp_path / 'outside'
    outside.mkdir()
    monkeypatch.chdir(outside)
    assert testee.main([]) == 2
    output = capsys.readouterr()
    assert output.out == ''
    assert 'snapshot:' in output.err


def test_script_entry_point_runs_from_repository_subdirectory(repo, monkeypatch, capsys):
    """The executable entry point discovers the repository and emits JSON."""
    root, _ = repo
    monkeypatch.chdir(root / 'dir')
    monkeypatch.setattr(sys, 'argv', [str(SCRIPT)])
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(SCRIPT), run_name='__main__')
    assert exit_info.value.code == 0
    assert json.loads(capsys.readouterr().out)['entries'] == []
