"""Exercise persistent shared-tool setup with owned repositories and no external commands."""

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

DEVCONTAINER = Path(__file__).resolve().parents[2] / '.devcontainer'
ORIGIN = 'https://github.com/liboyin/agent-workflow-common.git'


def git(root, *args):
    """Run Git with an owned home and no inherited configuration or repository overrides."""
    return subprocess.run(
        ['git', '-c', 'user.name=test', '-c', 'user.email=test@example.invalid', '-C', str(root), *args],
        check=True, capture_output=True, text=True,
        env={
            'PATH': os.defpath, 'HOME': str(root), 'LC_ALL': 'C.UTF-8',
            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': str(root / 'absent-global-config'),
        },
    ).stdout


@pytest.fixture
def setup(run_shell_script):
    """Provide real local clone inputs and stubs that cannot contact GitHub or install software."""
    runner = run_shell_script
    seed = runner.cwd / 'seed'
    (seed / 'tools').mkdir(parents=True)
    for name in ('check_docs.py', 'snapshot.py', 'dispatch.py'):
        (seed / 'tools' / name).write_text('# owned tool\n', encoding='utf-8')
    git(seed, 'init', '-b', 'main')
    git(seed, 'add', 'tools')
    git(seed, 'commit', '-m', 'owned seed')
    checkout = runner.home / 'persist' / 'agent-workflow-common'
    checkout.parent.mkdir()
    workspace = runner.cwd / 'workspaces'
    workspace.mkdir()
    link = workspace / 'agent-workflow-common'
    scripts = runner.cwd / '.devcontainer'
    scripts.mkdir()
    for name in ('setup-agent-tools.sh', 'setup-github-ssh.sh', 'postCreateCommand.sh'):
        shutil.copy2(DEVCONTAINER / name, scripts / name)
    bin_dir = runner.cwd / 'bin'
    bin_dir.mkdir()
    calls = runner.cwd / 'git-calls'
    real_git = shutil.which('git')
    assert real_git is not None
    # Every clone, including a mutated URL, is intercepted before Git can reach the network.
    stub = bin_dir / 'git'
    stub.write_text(
        '#!/bin/bash\nset -eu\n'
        f'printf "%s\\n" "$*" >> {shlex.quote(str(calls))}\n'
        'if [ "$1" = clone ]; then\n'
        '  [ -z "${REQUIRE_CHOWN_LOG:-}" ] || [ -s "$REQUIRE_CHOWN_LOG" ] || exit 92\n'
        f'  [ "$2" = {shlex.quote(ORIGIN)} ] || exit 91\n'
        '  [ "${CLONE_STATUS:-0}" = 0 ] || exit "$CLONE_STATUS"\n'
        f'  exec {shlex.quote(real_git)} clone {shlex.quote(str(seed))} "$3"\n'
        'fi\n'
        f'exec {shlex.quote(real_git)} "$@"\n', encoding='utf-8',
    )
    stub.chmod(0o755)
    return runner, checkout, link, scripts, bin_dir, calls


def run_setup(setup, *, status=0):
    """Run the helper with only owned paths and the local transport stub."""
    runner, checkout, link, scripts, bin_dir, _ = setup
    return runner(
        scripts / 'setup-agent-tools.sh', str(checkout), str(link), path_dirs=(bin_dir,),
        env={'CLONE_STATUS': str(status), 'GIT_CONFIG_NOSYSTEM': '1'},
    )


def test_first_setup_clones_tools_and_exposes_the_sibling_link(setup):
    """A fresh setup provides all shared tools through the expected sibling path."""
    _, checkout, link, _, _, calls = setup
    result = run_setup(setup)
    assert result.returncode == 0, result.stderr
    assert link.is_symlink() and link.resolve() == checkout
    assert (link / 'tools/check_docs.py').read_text() == '# owned tool\n'
    assert calls.read_text().splitlines()[0] == f'clone {ORIGIN} {checkout}'


def test_repeat_setup_preserves_local_work_without_fetching_or_resetting(setup):
    """Repeated setup keeps the checkout's commit, dirty files and untracked files intact."""
    _, checkout, link, _, _, calls = setup
    assert run_setup(setup).returncode == 0
    head = git(checkout, 'rev-parse', 'HEAD')
    tool = checkout / 'tools/check_docs.py'
    tool.write_text('# uncommitted work\n')
    (checkout / 'notes').write_text('untracked work\n')
    calls.write_text('')
    result = run_setup(setup)
    assert result.returncode == 0, result.stderr
    assert git(checkout, 'rev-parse', 'HEAD') == head
    assert tool.read_text() == '# uncommitted work\n'
    assert (checkout / 'notes').read_text() == 'untracked work\n'
    assert link.resolve() == checkout
    assert calls.read_text().splitlines() == [f'-C {checkout} rev-parse --is-inside-work-tree']


@pytest.mark.parametrize('kind', ['directory', 'file', 'dangling_link'])
def test_conflicting_sibling_paths_are_preserved_before_cloning(setup, kind):
    """Setup refuses occupied destinations without modifying them or cloning elsewhere."""
    runner, checkout, link, _, _, calls = setup
    if kind == 'directory':
        link.mkdir()
        (link / 'notes').write_text('keep directory\n')
    elif kind == 'file':
        link.write_text('keep file\n')
    else:
        link.symlink_to(runner.cwd / 'missing')
    result = run_setup(setup)
    assert result.returncode != 0
    assert 'already exists' in result.stderr
    assert not checkout.exists() and not calls.exists()
    if kind == 'directory':
        assert (link / 'notes').read_text() == 'keep directory\n'
    elif kind == 'file':
        assert link.read_text() == 'keep file\n'
    else:
        assert link.is_symlink() and link.readlink() == runner.cwd / 'missing'


def test_failed_clone_propagates_failure_without_publishing_a_link(setup):
    """A failed clone cannot be reported as successful setup or publish a broken link."""
    _, checkout, link, _, _, _ = setup
    result = run_setup(setup, status=42)
    assert result.returncode == 42
    assert not checkout.exists()
    assert not link.exists() and not link.is_symlink()


def test_existing_non_repository_is_preserved_and_reported(setup):
    """A populated non-repository is never replaced to make room for a clone."""
    _, checkout, link, _, _, calls = setup
    checkout.mkdir()
    (checkout / 'notes').write_text('keep\n')
    result = run_setup(setup)
    assert result.returncode != 0
    assert 'not a Git checkout' in result.stderr
    assert (checkout / 'notes').read_text() == 'keep\n'
    assert not link.is_symlink() and not calls.exists()


def test_missing_tool_stops_setup_without_repairing_the_checkout(setup):
    """An incomplete checkout requires explicit repair instead of an automatic update."""
    _, checkout, link, _, _, _ = setup
    assert run_setup(setup).returncode == 0
    link.unlink()
    (checkout / 'tools/snapshot.py').unlink()
    result = run_setup(setup)
    assert result.returncode != 0
    assert 'snapshot.py is missing' in result.stderr
    assert not link.is_symlink()
    assert not (checkout / 'tools/snapshot.py').exists()


def test_post_create_initializes_tools_after_nonrecursive_workspace_chown(setup):
    """The creation hook uses the persistent path and changes only the workspace parent's ownership."""
    runner, checkout, link, scripts, bin_dir, _ = setup
    hook = scripts / 'postCreateCommand.sh'
    # Relocate the sole absolute workspace path before any shell command can run.
    hook.write_text(hook.read_text().replace('/workspaces', str(link.parent)))
    sudo_calls = runner.cwd / 'sudo-calls'
    stubs = {
        'uv': 'exit 0\n',
        'sudo': f'printf "%s\\n" "$*" >> {shlex.quote(str(sudo_calls))}\n',
        'curl': 'case "$*" in *api.github.com/meta*) echo \'{"ssh_keys":["owned-key"]}\';; esac\n',
        'codex': f'test -f {shlex.quote(str(link / "tools/check_docs.py"))}\n',
    }
    for name, body in stubs.items():
        stub = bin_dir / name
        stub.write_text('#!/bin/bash\nset -eu\n' + body)
        stub.chmod(0o755)
    result = runner(hook, path_dirs=(bin_dir,), env={
        'CLAUDE_CONFIG_DIR': str(runner.home / 'persist/claude'),
        'CODEX_HOME': str(runner.home / 'persist/codex'),
        'REQUIRE_CHOWN_LOG': str(sudo_calls),
        'GIT_CONFIG_NOSYSTEM': '1',
    })
    assert result.returncode == 0, result.stderr
    assert sudo_calls.read_text().splitlines() == [f'chown ubuntu:ubuntu {link.parent}']
    assert link.is_symlink() and link.resolve() == checkout
    assert (runner.home / '.ssh/known_hosts').read_text() == 'github.com owned-key\n'


def test_both_devcontainers_use_the_hook_without_a_shared_tools_bind_mount():
    """Both variants initialize shared tools through the hook without requiring a host checkout."""
    for name in ('devcontainer.json', 'real-data/devcontainer.json'):
        text = '\n'.join(line for line in (DEVCONTAINER / name).read_text().splitlines()
                         if not line.lstrip().startswith('//'))
        config = json.loads(text)
        assert config['postCreateCommand'].endswith('./.devcontainer/postCreateCommand.sh')
        assert all('agent-workflow-common' not in mount for mount in config['mounts'])


def test_git_fixture_does_not_read_inherited_configuration(setup, monkeypatch):
    """An inherited global config cannot affect the Git fixture's owned repository."""
    runner, _, _, _, _, _ = setup
    config = runner.cwd / 'parent.gitconfig'
    config.write_text('[audit]\n    marker = inherited\n')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(config))
    with pytest.raises(subprocess.CalledProcessError) as error:
        git(runner.cwd / 'seed', 'config', '--get', 'audit.marker')
    assert error.value.returncode == 1
