"""Exercise the review tools with owned Git repositories, stub commands and nested pytest runs."""

import importlib.util
import json
import runpy
import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REFERENCES = Path(__file__).resolve().parents[2] / '.agents/skills/adversarial-review/references'


def load(name):
    """Load a standalone review script by file path; AGENTS permits this for scripts outside the package."""
    spec = importlib.util.spec_from_file_location(f'review_{name}', REFERENCES / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mutate = load('mutate')
dispatch = load('dispatch')
worktree_gates = load('worktree_gates')

TEST_SOURCE = '''from pathlib import Path

from youtube_collector.calc import add


def test_add():
    """Add adds."""
    assert add(1, 2) == 3


def test_ignored_files_are_not_copied():
    """The copy holds no ignored content."""
    assert not Path('.env').exists()
'''


def git(root, *args):
    """Run git in an owned repository with a fixed identity and return its output."""
    return subprocess.run(
        ['git', '-c', 'user.name=t', '-c', 'user.email=t@example.invalid', *args], cwd=root, check=True,
        capture_output=True, text=True,
    ).stdout


def make_repo(root):
    """Create a repository with a small package, its test and an ignored file, committed."""
    (root / 'youtube_collector').mkdir(parents=True)
    (root / 'tests').mkdir()
    (root / 'youtube_collector/__init__.py').touch()
    (root / 'youtube_collector/calc.py').write_text('def add(a, b):\n    return a + b\n', encoding='utf-8')
    (root / 'tests/test_calc.py').write_text(TEST_SOURCE, encoding='utf-8')
    (root / '.gitignore').write_text('.env\n', encoding='utf-8')
    (root / '.env').write_text('SECRET=1\n', encoding='utf-8')
    git(root, 'init', '-q', '-b', 'main')
    git(root, 'add', '.gitignore', 'youtube_collector', 'tests')
    git(root, 'commit', '-q', '-m', 'start')
    return root


@pytest.fixture
def repo(tmp_path):
    """Provide an owned repository."""
    return make_repo(tmp_path / 'repo')


CALC = 'youtube_collector/calc.py'
TESTS = ['tests/test_calc.py']


def write_mutants(path, *mutants):
    """Write a mutants file holding the given tuples."""
    path.write_text(f'MUTANTS = {mutants!r}\n', encoding='utf-8')
    return path


def run_batch(tmp_path, repo, *mutants, extra=()):
    """Run a mutants file through the command line; return its status and the evidence path."""
    evidence = tmp_path / 'evidence.txt'
    mutants_file = write_mutants(tmp_path / 'mutants.py', *mutants)
    arguments = [str(mutants_file), '--evidence', str(evidence), '--repo', str(repo)]
    return mutate.main([*arguments, *extra]), evidence


def test_mutate_reports_a_killed_mutant_with_its_evidence(tmp_path, repo, capsys):
    """A mutant that fails its test is killed; the evidence carries provenance and leaves the repo alone."""
    status, evidence = run_batch(tmp_path, repo, ('M1', 'adds', CALC, 'a + b', 'a - b', TESTS))
    text = evidence.read_text(encoding='utf-8')
    assert status == 0
    assert '[KILLED]' in text
    assert text.count('ACTUAL_IMPORT_PROVENANCE') == 2  # the baseline and the mutant
    assert 'modules=0' not in text
    assert 'CANDIDATE sha256=' in text
    assert 'randomly-seed=' in text
    assert 'BEHAVIORAL tests/test_calc.py::test_add' in text
    assert 'assert -1 == 3' in text
    assert 'threads=[]' in text
    assert 'FAILED tests/test_calc.py::test_add' in text
    assert 'a + b' in (repo / CALC).read_text(encoding='utf-8')
    assert 'Not killed: 0' in capsys.readouterr().out


def test_mutate_gives_tests_an_owned_environment(tmp_path, repo, monkeypatch):
    """Inherited settings do not reach the tests, and home and temporary files stay in the owned copy."""
    monkeypatch.setenv('COLLECTOR_STAGING_DIR', '/real/share')
    (repo / 'tests/test_environment.py').write_text('''import os
import tempfile
from pathlib import Path


def test_environment():
    \"\"\"Nothing leaks in.\"\"\"
    owned = Path(os.environ['MUTATE_SCRATCH']).parent / 'owned'
    assert 'COLLECTOR_STAGING_DIR' not in os.environ
    assert Path(os.environ['HOME']) == owned
    assert Path(tempfile.gettempdir()) == owned
''', encoding='utf-8')
    status, evidence = run_batch(tmp_path, repo, (
        'M1', 'adds', CALC, 'a + b', 'a - b', ['tests/test_calc.py', 'tests/test_environment.py'],
    ), extra=('--untracked', 'tests/test_environment.py'))
    assert status == 0, evidence.read_text(encoding='utf-8') if evidence.exists() else 'no evidence'


@pytest.mark.parametrize('run', [
    mutate.Run(0, '1 passed', None, ()), mutate.Run(1, '1 failed', None, ('FAILED t',)),
    mutate.Run(None, 'timed out', None, ()), mutate.Run(1, '1 error', 'seen', ('ERROR t',)),
    mutate.Run(4, 'usage', 'seen', ()), mutate.Run(1, 'no list', 'seen', ()),
    mutate.Run(1, 'no reason known', 'seen', ('FAILED t',)),
    mutate.Run(1, 'broken', 'seen', ('FAILED t',), ('NON_BEHAVIORAL t: SyntaxError',)),
    mutate.Run(1, 'mixed', 'seen', ('FAILED t', 'ERROR u'), ('BEHAVIORAL t: boom',)),
])
def test_verdict_is_invalid_without_provenance_or_behavioral_failures(run):
    """Only a failure of a test, with provenance verified, is evidence of a kill."""
    assert mutate.verdict(run) == 'INVALID'


def test_verdict_tells_survivors_from_kills():
    """A clean run survived; a behavioral failure with provenance killed."""
    assert mutate.verdict(mutate.Run(0, '1 passed', 'seen', ())) == 'SURVIVED'
    assert mutate.verdict(mutate.Run(1, '1 failed', 'seen', ('FAILED t',), ('BEHAVIORAL t: boom',))) == 'KILLED'


def test_mutate_restores_the_copy_between_mutants(tmp_path, repo):
    """Each mutant starts from the candidate: a second, equivalent mutant does not inherit the first."""
    status, evidence = run_batch(
        tmp_path, repo, ('M1', 'adds', CALC, 'a + b', 'a - b', TESTS), ('M2', 'same', CALC, 'a + b', 'b + a', TESTS),
    )
    text = evidence.read_text(encoding='utf-8')
    assert status == 1
    assert 'M1 [KILLED]' in text
    assert 'M2 [SURVIVED]' in text


def test_mutate_counts_a_collection_error_as_invalid(tmp_path, repo):
    """A mutant that breaks imports proves nothing about behavior."""
    status, evidence = run_batch(tmp_path, repo, ('M1', 'adds', CALC, 'a + b', 'a +', TESTS))
    assert status == 1
    assert '[INVALID]' in evidence.read_text(encoding='utf-8')


@pytest.mark.parametrize('inside_repository', [True, False])
def test_mutate_counts_an_escaped_import_as_invalid(tmp_path, repo, inside_repository):
    """A run that loads the application from the original repository or anywhere else is invalid."""
    outside = (repo if inside_repository else tmp_path) / 'outside'
    (outside / 'youtube_collector').mkdir(parents=True)
    (outside / 'youtube_collector/__init__.py').touch()
    (outside / 'youtube_collector/calc.py').write_text('def add(a, b):\n    return 0\n', encoding='utf-8')
    escape = f'import sys\nsys.path.insert(0, {str(outside)!r})\nfrom youtube_collector.calc import add'
    status, evidence = run_batch(tmp_path, repo, (
        'M1', 'adds', 'tests/test_calc.py', 'from youtube_collector.calc import add', escape, TESTS,
    ))
    assert status == 1
    assert '[INVALID]' in evidence.read_text(encoding='utf-8')


def test_mutate_counts_any_module_from_the_original_repository_as_invalid(tmp_path, repo):
    """Provenance covers more than the application: a helper from the original repository also escapes."""
    (repo / 'outside').mkdir()
    (repo / 'outside/helper_module.py').touch()
    escape = (
        f'import sys\nsys.path.insert(0, {str(repo / "outside")!r})\nimport helper_module\n'
        'from youtube_collector.calc import add'
    )
    status, evidence = run_batch(tmp_path, repo, (
        'M1', 'adds', 'tests/test_calc.py', 'from youtube_collector.calc import add', escape, TESTS,
    ))
    assert status == 1
    assert '[INVALID]' in evidence.read_text(encoding='utf-8')


def test_run_tests_ignores_a_report_left_by_an_earlier_run(tmp_path):
    """Evidence is only ever this process's own: a run that writes none has no provenance."""
    owned = tmp_path / 'owned'
    owned.mkdir()
    stale = 'ACTUAL_IMPORT_PROVENANCE stale\nFAILURE_DETAIL BEHAVIORAL t: x\n'
    (owned / 'report.txt').write_text(stale, encoding='utf-8')
    done = subprocess.CompletedProcess([], 1, 'FAILED t\n1 failed\n', '')
    with patch.object(mutate.subprocess, 'run', return_value=done):
        run = mutate.run_tests(tmp_path, owned, tmp_path, tmp_path, ['t'], 5.0)
    assert (run.provenance, run.details) == (None, ())
    assert mutate.verdict(run) == 'INVALID'


def test_mutate_counts_broken_code_in_a_test_body_as_invalid(tmp_path, repo):
    """A SyntaxError raised while a test runs is reported as FAILED but proves nothing about behavior."""
    (repo / 'tests/test_calc.py').write_text('''def test_add():
    \"\"\"Import late, so a broken module fails the test rather than collection.\"\"\"
    from youtube_collector.calc import add
    assert add(1, 2) == 3
''', encoding='utf-8')
    status, evidence = run_batch(tmp_path, repo, ('M1', 'adds', CALC, 'a + b', 'a +', TESTS))
    text = evidence.read_text(encoding='utf-8')
    assert status == 1
    assert '[INVALID]' in text
    assert 'NON_BEHAVIORAL tests/test_calc.py::test_add' in text


def test_mutate_counts_a_timeout_as_invalid(tmp_path, repo):
    """A mutant that hangs is reported rather than waited on; the batch carries on."""
    real = subprocess.run
    calls = []

    def run(command, **keywords):
        if keywords.get('timeout') is not None:
            calls.append(command)
            if len(calls) == 2:  # the baseline is the first
                raise subprocess.TimeoutExpired(command, keywords['timeout'])
        return real(command, **keywords)

    with patch.object(mutate.subprocess, 'run', run):
        status, evidence = run_batch(tmp_path, repo, ('M1', 'adds', CALC, 'a + b', 'a - b', TESTS))
    text = evidence.read_text(encoding='utf-8')
    assert status == 1
    assert '[INVALID]' in text
    assert 'timed out' in text


@pytest.mark.parametrize(('old', 'count'), [('absent text', 0), ('a', 3)])
def test_mutate_refuses_old_text_that_is_not_unique(tmp_path, repo, capsys, old, count):
    """A mutant whose old text does not occur exactly once stops the batch before anything runs."""
    status, evidence = run_batch(tmp_path, repo, ('M1', 'adds', CALC, old, 'x', TESTS))
    assert status == 2
    assert f'occurs {count} times' in capsys.readouterr().err
    assert not evidence.exists()


def test_mutate_refuses_a_failing_baseline(tmp_path, repo, capsys):
    """Kills mean nothing against a failing baseline."""
    (repo / CALC).write_text('def add(a, b):\n    return 0\n', encoding='utf-8')
    status, _ = run_batch(tmp_path, repo, ('M1', 'adds', CALC, 'return 0', 'return 1', TESTS))
    assert status == 2
    assert 'Baseline' in capsys.readouterr().err


def test_mutate_copies_tracked_files_and_only_the_named_untracked_ones(repo):
    """Unnamed untracked content stays out, deleted paths are skipped, and dirty content is read as it is."""
    (repo / 'named.txt').write_text('new', encoding='utf-8')
    (repo / 'unrelated.txt').write_text('skip', encoding='utf-8')
    (repo / 'tests/test_calc.py').unlink()
    names = mutate.candidate_files(repo, frozenset({'named.txt'}))
    assert 'named.txt' in names
    assert CALC in names
    assert 'unrelated.txt' not in names
    assert 'tests/test_calc.py' not in names


@pytest.mark.parametrize('name', ['.env', 'missing.txt'])
def test_mutate_refuses_a_named_path_that_is_ignored_or_absent(repo, name):
    """Naming an ignored file does not make it copyable."""
    with pytest.raises(ValueError, match='Not untracked'):
        mutate.candidate_files(repo, frozenset({name}))


def test_mutate_recreates_relative_symlinks(tmp_path, repo):
    """A repository-relative symlink stays a symlink in the copy."""
    (repo / 'link').symlink_to('youtube_collector')
    tree = tmp_path / 'tree'
    mutate.copy_candidate(repo, tree, ['link'])
    assert (tree / 'link').is_symlink()


def test_mutate_refuses_a_symlink_out_of_the_repository(tmp_path, repo):
    """A link leaving the repository would escape the copy."""
    (repo / 'link').symlink_to(tmp_path)
    with pytest.raises(ValueError, match='out of the repository'):
        mutate.copy_candidate(repo, tmp_path / 'tree', ['link'])


def test_mutate_refuses_an_absolute_symlink_even_into_the_repository(tmp_path, repo):
    """Writing through such a link in the copy would change the original."""
    (repo / 'link').symlink_to(repo / CALC)
    with pytest.raises(ValueError, match='out of the repository'):
        mutate.copy_candidate(repo, tmp_path / 'tree', ['link'])


def test_mutate_refuses_a_path_below_a_symlink(tmp_path, repo):
    """A parent symlink would pull outside content into the copy."""
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'data').write_text('outside', encoding='utf-8')
    (repo / 'link').symlink_to(outside)
    with pytest.raises(ValueError, match='below a symlink'):
        mutate.copy_candidate(repo, tmp_path / 'tree', ['link/data'])
    assert not (tmp_path / 'tree/link/data').exists()


def test_mutate_refuses_an_unloadable_mutants_file(tmp_path):
    """A path Python cannot treat as a module is a clean error."""
    with pytest.raises(ValueError, match='Cannot load'):
        mutate.load_mutants(tmp_path / 'mutants.txt')


def stub_codex(tmp_path, monkeypatch, *events, status=0):
    """Put a ``codex`` stub first on PATH that records its arguments."""
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    record = tmp_path / 'codex-args'
    output = '\n'.join(json.dumps(event) for event in events) + '\nnot json\n'
    stub = bin_dir / 'codex'
    stub.write_text(
        '#!/bin/bash\n'
        f'printf "%s\\n" "$@" > {shlex.quote(str(record))}\n'
        f'printf "%s" {shlex.quote(output)}\n'
        f'exit {status}\n',
        encoding='utf-8',
    )
    stub.chmod(0o755)
    monkeypatch.setenv('PATH', str(bin_dir), prepend=':')
    return record


STARTED = {'type': 'thread.started', 'thread_id': 'abc'}
REPORT = {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'No blocking findings.'}}
FIRST = ['--record', 'rec.md', '--boundary', 'tasks/open/T1.md', '--purpose', 'Do a thing.']


def test_dispatch_reads_the_base_revision_from_the_target(tmp_path, repo, monkeypatch, capsys):
    """The prompt names the target's actual HEAD, the dispatch arguments and closed stdin."""
    record = stub_codex(tmp_path, monkeypatch, STARTED, REPORT)
    log = tmp_path / 'log.jsonl'
    with patch.object(dispatch.subprocess, 'run', wraps=subprocess.run) as run:
        status = dispatch.main([*FIRST, '--target', str(repo), '--log', str(log), '--ignore', 'junk.sh'])
    # An open non-terminal stdin makes the CLI wait for more prompt text instead of starting.
    assert run.call_args_list[-1].kwargs['stdin'] is subprocess.DEVNULL
    arguments = record.read_text(encoding='utf-8')
    assert status == 0
    assert git(repo, 'rev-parse', 'HEAD').strip() in arguments
    assert 'gpt-6.1-sol' in arguments
    assert 'model_reasoning_effort="medium"' in arguments
    assert 'Unrelated paths to ignore: junk.sh.' in arguments
    assert 'tasks/open/T1.md' in arguments
    assert capsys.readouterr().out == 'SESSION abc\nNo blocking findings.\n'
    assert 'thread.started' in log.read_text(encoding='utf-8')


def test_dispatch_resumes_the_recorded_reviewer(tmp_path, repo, monkeypatch):
    """A follow-up continues the named session rather than starting another."""
    record = stub_codex(tmp_path, monkeypatch, REPORT)
    status = dispatch.main([
        '--record', 'rec.md', '--target', str(repo), '--log', str(tmp_path / 'log'),
        '--resume', 'abc', '--delta', 'A fix.',
    ])
    lines = record.read_text(encoding='utf-8').splitlines()
    assert status == 0
    assert lines[:2] == ['exec', 'resume']
    assert 'abc' in lines
    assert any('A fix.' in line for line in lines)


def test_dispatch_propagates_the_cli_status(tmp_path, repo, monkeypatch):
    """A failed CLI run is a failed dispatch, even with a partial report."""
    stub_codex(tmp_path, monkeypatch, REPORT, status=3)
    assert dispatch.main([*FIRST, '--target', str(repo), '--log', str(tmp_path / 'log')]) == 3


def test_dispatch_fails_without_a_report(tmp_path, repo, monkeypatch, capsys):
    """A successful exit with no report is not a review."""
    stub_codex(tmp_path, monkeypatch, STARTED)
    assert dispatch.main([*FIRST, '--target', str(repo), '--log', str(tmp_path / 'log')]) == 1
    assert 'no report' in capsys.readouterr().err


@pytest.mark.parametrize('arguments', [
    ['--record', 'r', '--log', 'l'], ['--record', 'r', '--log', 'l', '--resume', 'abc'],
])
def test_dispatch_requires_the_right_arguments_for_its_mode(arguments, capsys):
    """A first assessment needs its boundary and purpose, and a follow-up its delta."""
    with pytest.raises(SystemExit) as raised:
        dispatch.main(arguments)
    assert raised.value.code == 2
    assert 'needs' in capsys.readouterr().err


def gate_stubs(repo, monkeypatch, tmp_path, *, coverage=0, mypy=0):
    """Commit a fake coverage gate and put mypy and ruff stubs first on PATH; each records its run."""
    calls = tmp_path / 'calls'
    template = f'#!/bin/bash\necho "$0 $PWD $PYTHONPATH" >> {shlex.quote(str(calls))}\nexit %d\n'
    script = repo / 'check-coverage.sh'
    script.write_text(template % coverage, encoding='utf-8')
    script.chmod(0o755)
    git(repo, 'add', 'check-coverage.sh')
    git(repo, 'commit', '-q', '-m', 'gate')
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    for name, status in (('mypy', mypy), ('ruff', 0)):
        (bin_dir / name).write_text(template % status, encoding='utf-8')
        (bin_dir / name).chmod(0o755)
    monkeypatch.setenv('PATH', str(bin_dir), prepend=':')
    return calls


def test_worktree_gates_freeze_the_candidate_and_run_each_gate_there(tmp_path, repo, monkeypatch, capsys):
    """Only the named files reach the worktree, deletions included, and gates see its PYTHONPATH."""
    calls = gate_stubs(repo, monkeypatch, tmp_path)
    (repo / CALC).write_text('def add(a, b):\n    return b + a\n', encoding='utf-8')
    (repo / 'tests/test_calc.py').write_text('changed but not named\n', encoding='utf-8')
    (repo / 'tests/new.txt').write_text('new', encoding='utf-8')
    (repo / '.gitignore').unlink()
    worktree = (tmp_path / 'wt').resolve()
    status = worktree_gates.main([str(worktree), CALC, 'tests/new.txt', '.gitignore', '--repo', str(repo)])
    assert status == 0
    assert 'b + a' in (worktree / CALC).read_text(encoding='utf-8')
    assert (worktree / 'tests/new.txt').exists()
    assert not (worktree / '.gitignore').exists()
    assert (worktree / 'tests/test_calc.py').read_text(encoding='utf-8') == TEST_SOURCE
    lines = calls.read_text(encoding='utf-8').splitlines()
    assert [Path(line.split()[0]).name for line in lines] == ['check-coverage.sh', 'mypy', 'ruff']
    assert all(line.split()[1:] == [str(worktree), str(worktree)] for line in lines)
    assert capsys.readouterr().out.count('PASSED') == 3


def test_worktree_gates_run_every_gate_and_report_a_failure(tmp_path, repo, monkeypatch, capsys):
    """One failing gate fails the run but does not hide the others."""
    calls = gate_stubs(repo, monkeypatch, tmp_path, mypy=1)
    assert worktree_gates.main([str(tmp_path / 'wt'), CALC, '--repo', str(repo)]) == 1
    assert len(calls.read_text(encoding='utf-8').splitlines()) == 3
    assert 'FAILED mypy youtube_collector' in capsys.readouterr().out


@pytest.mark.parametrize('absolute', [False, True])
def test_worktree_gates_refuse_paths_outside_the_repository(tmp_path, repo, monkeypatch, capsys, absolute):
    """A candidate path must stay inside the repository, and an owned file there is left alone."""
    gate_stubs(repo, monkeypatch, tmp_path)
    sentinel = tmp_path / 'outside'
    sentinel.write_text('keep', encoding='utf-8')
    name = str(sentinel) if absolute else '../outside'  # relative to the worktree, tmp_path/wt
    assert worktree_gates.main([str(tmp_path / 'wt'), name, '--repo', str(repo)]) == 2
    assert 'not a relative path' in capsys.readouterr().err
    assert sentinel.read_text(encoding='utf-8') == 'keep'
    assert not (tmp_path / 'wt').exists()


def test_worktree_gates_refuse_a_path_below_a_symlink_in_the_repository(tmp_path, repo, monkeypatch, capsys):
    """A symlinked parent must not redirect a removal outside the tree; the sentinel survives."""
    gate_stubs(repo, monkeypatch, tmp_path)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'data').write_text('keep', encoding='utf-8')
    (repo / 'link').symlink_to(outside)
    assert worktree_gates.main([str(tmp_path / 'wt'), 'link/data', '--repo', str(repo)]) == 2
    assert 'below a symlink' in capsys.readouterr().err
    assert (outside / 'data').read_text(encoding='utf-8') == 'keep'
    assert not (tmp_path / 'wt').exists()


def test_worktree_gates_refuse_a_path_below_a_symlink_in_the_worktree(tmp_path, repo, monkeypatch, capsys):
    """A committed symlink, replaced by a directory in the repository, is caught in the new worktree."""
    gate_stubs(repo, monkeypatch, tmp_path)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'data').write_text('keep', encoding='utf-8')
    (repo / 'link').symlink_to(outside)
    git(repo, 'add', 'link')
    git(repo, 'commit', '-q', '-m', 'link')
    (repo / 'link').unlink()
    (repo / 'link').mkdir()
    (repo / 'link/data').write_text('new', encoding='utf-8')
    assert worktree_gates.main([str(tmp_path / 'wt'), 'link/data', '--repo', str(repo)]) == 2
    assert 'below a symlink' in capsys.readouterr().err
    assert (outside / 'data').read_text(encoding='utf-8') == 'keep'
    assert not (tmp_path / 'wt').exists()


def test_worktree_gates_report_a_worktree_that_cannot_be_made(tmp_path, repo, capsys):
    """An existing, non-empty destination is a clean error, and its contents are kept."""
    (tmp_path / 'wt').mkdir()
    (tmp_path / 'wt/keep').touch()
    assert worktree_gates.main([str(tmp_path / 'wt'), CALC, '--repo', str(repo)]) == 2
    assert 'worktree_gates:' in capsys.readouterr().err
    assert (tmp_path / 'wt/keep').exists()


@pytest.mark.parametrize('name', ['mutate', 'dispatch', 'worktree_gates'])
def test_scripts_run_as_programs(name, monkeypatch, capsys):
    """Each script's entry point runs its command line; --help exits cleanly."""
    monkeypatch.setattr(sys, 'argv', [name, '--help'])
    with pytest.raises(SystemExit) as raised:
        runpy.run_path(str(REFERENCES / f'{name}.py'), run_name='__main__')
    assert raised.value.code == 0
    assert 'usage' in capsys.readouterr().out
