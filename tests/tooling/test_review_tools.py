"""Exercise the review tools with owned Git repositories, stub commands and nested pytest runs."""

import importlib.util
import os
import runpy
import shlex
import subprocess
import sys
import tempfile
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
    assert 'advice:' not in text
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
    text = evidence.read_text(encoding='utf-8')
    assert status == 1
    assert '[INVALID]' in text
    assert 'advice: ' in text


@pytest.mark.parametrize(('run', 'expected'), [
    (mutate.Run(None, 'timed out', None, ()), 'narrowest tests'),
    (mutate.Run(1, '1 failed', None, ('FAILED t',)), 'provenance was not verified'),
    (mutate.Run(4, 'usage', 'seen', ()), 'status 4'),
    (mutate.Run(1, 'mixed', 'seen', ('FAILED t', 'ERROR u'), ('BEHAVIORAL t: boom',)), 'in a finally'),
    (mutate.Run(1, 'broken', 'seen', ('FAILED t',), ('NON_BEHAVIORAL t: SyntaxError',)), 'broken code'),
    (mutate.Run(1, 'no reason known', 'seen', ('FAILED t',)), 'no failure reason'),
])
def test_advice_names_the_reason_a_run_is_not_evidence(run, expected):
    """Each kind of invalid run gets the remedy that fits it."""
    assert expected in mutate.advice(run)


def focused(tmp_path, repo, *arguments):
    """Run tests with no mutant through the command line; return the status and the evidence text."""
    evidence = tmp_path / 'focused.txt'
    status = mutate.main(['--evidence', str(evidence), '--repo', str(repo), *arguments])
    return status, evidence.read_text(encoding='utf-8') if evidence.exists() else ''


def test_mutate_runs_tests_with_no_mutant(tmp_path, repo, capsys):
    """A focused run passes on the unmutated copy, with the candidate's identity and provenance."""
    status, text = focused(tmp_path, repo, '--tests', 'tests/test_calc.py')
    assert status == 0
    assert 'CANDIDATE sha256=' in text
    assert "FOCUSED ['tests/test_calc.py'] exit=0 2 passed" in text
    assert 'ACTUAL_IMPORT_PROVENANCE' in text
    assert 'modules=0' not in text
    assert 'advice:' not in text
    assert 'FOCUSED' in capsys.readouterr().out


def test_mutate_reports_a_failing_focused_run(tmp_path, repo):
    """A focused run of a dirty candidate that fails its test exits with one and says why."""
    (repo / CALC).write_text('def add(a, b):\n    return 0\n', encoding='utf-8')
    status, text = focused(tmp_path, repo, '--tests', 'tests/test_calc.py')
    assert status == 1
    assert 'BEHAVIORAL tests/test_calc.py::test_add' in text
    assert 'advice:' not in text


def test_mutate_counts_a_focused_run_that_ran_nothing_as_not_evidence(tmp_path, repo):
    """A selection pytest cannot run is neither a pass nor a failure."""
    status, text = focused(tmp_path, repo, '--tests', 'tests/test_absent.py')
    assert status == 2
    assert 'advice: ' in text


@pytest.mark.parametrize(('run', 'reason'), [
    (mutate.Run(None, 'timed out after 5 s', None, ()), 'the run timed out'),
    (mutate.Run(0, '2 passed', None, ()), 'provenance was not verified'),
    (mutate.Run(1, '1 failed', None, ('FAILED t',)), 'provenance was not verified'),
    (mutate.Run(4, 'no tests ran', 'seen', ()), 'pytest exited with status 4'),
])
def test_mutate_counts_an_unverified_focused_run_as_not_evidence(tmp_path, repo, run, reason):
    """A timeout, a run without provenance or a pytest usage error exits with two and gives the remedy."""
    with patch.object(mutate, 'run_tests', return_value=run):
        status, text = focused(tmp_path, repo, '--tests', 'tests/test_calc.py')
    assert status == 2
    assert f'advice: {reason}' in text


def test_mutate_adds_a_probe_file_to_the_copy_only(tmp_path, repo):
    """A probe test reaches the copy and runs there; the repository gains nothing."""
    probe = tmp_path / 'probe.py'
    probe.write_text(
        'from youtube_collector.calc import add\n\n\ndef test_probe():\n    assert add(2, 2) == 5\n', encoding='utf-8',
    )
    status, text = focused(tmp_path, repo, '--tests', 'tests/test_probe.py', '--add', f'{probe}=tests/test_probe.py')
    assert status == 1
    assert 'ADDED tests/test_probe.py sha256=' in text
    assert 'BEHAVIORAL tests/test_probe.py::test_probe' in text
    assert not (repo / 'tests/test_probe.py').exists()


def test_mutate_runs_mutants_against_an_added_probe(tmp_path, repo):
    """An added probe test can observe a mutant, and the evidence names the addition."""
    probe = tmp_path / 'probe.py'
    probe.write_text(
        'from youtube_collector.calc import add\n\n\ndef test_probe():\n    assert add(2, 3) == 5\n', encoding='utf-8',
    )
    status, evidence = run_batch(
        tmp_path, repo, ('M1', 'adds', CALC, 'a + b', 'a - b', ['tests/test_probe.py']),
        extra=('--add', f'{probe}=tests/test_probe.py'),
    )
    text = evidence.read_text(encoding='utf-8')
    assert status == 0
    assert 'ADDED tests/test_probe.py' in text
    assert 'M1 [KILLED]' in text


@pytest.mark.parametrize('value', ['no-separator', '=tests/x.py', '{source}=', '{source}=/abs.py', '{source}=../x.py'])
def test_mutate_refuses_a_malformed_addition(tmp_path, repo, capsys, value):
    """An addition needs a source and a relative path that stays inside the copy."""
    source = tmp_path / 'probe.py'
    source.touch()
    status, text = focused(tmp_path, repo, '--tests', 'tests/test_calc.py', '--add', value.format(source=source))
    assert status == 2
    assert 'expected SOURCE=PATH' in capsys.readouterr().err
    assert text == ''


@pytest.mark.parametrize('kind', ['missing', 'directory', 'symlink'])
def test_mutate_refuses_an_addition_that_is_not_a_regular_file(tmp_path, kind):
    """Only a regular file is copied: a link could pull in content the caller did not name."""
    source = tmp_path / 'source'
    if kind == 'directory':
        source.mkdir()
    elif kind == 'symlink':
        (tmp_path / 'real').touch()
        source.symlink_to(tmp_path / 'real')
    with pytest.raises(ValueError, match='not a regular file'):
        mutate.parse_additions([f'{source}=tests/x.py'])


def test_mutate_refuses_an_addition_below_a_symlink(tmp_path):
    """A symlinked parent in the copy must not redirect the addition outside it."""
    outside, tree, source = tmp_path / 'outside', tmp_path / 'tree', tmp_path / 'probe.py'
    outside.mkdir()
    tree.mkdir()
    source.write_text('probe', encoding='utf-8')
    (tree / 'link').symlink_to(outside)
    with pytest.raises(ValueError, match='below a symlink'):
        mutate.add_file(tree, source, 'link/probe.py')
    assert not (outside / 'probe.py').exists()


def test_mutate_replaces_a_symlink_instead_of_writing_through_it(tmp_path):
    """An addition at a symlink's path replaces the link; the link's target keeps its content."""
    outside, tree, source = tmp_path / 'outside.py', tmp_path / 'tree', tmp_path / 'probe.py'
    outside.write_text('keep', encoding='utf-8')
    tree.mkdir()
    source.write_text('probe', encoding='utf-8')
    (tree / 'link.py').symlink_to(outside)
    mutate.add_file(tree, source, 'link.py')
    assert not (tree / 'link.py').is_symlink()
    assert (tree / 'link.py').read_text(encoding='utf-8') == 'probe'
    assert outside.read_text(encoding='utf-8') == 'keep'


def test_candidate_digest_tells_apart_a_mode_change_and_a_kind_change(tmp_path):
    """Equal bytes still give distinct digests for another mode or for a symlink, which evidence identity needs."""
    path = tmp_path / 'candidate'
    path.write_text('target', encoding='utf-8')
    path.chmod(0o644)
    plain = mutate.candidate_digest(tmp_path, ['candidate'])
    path.chmod(0o755)
    executable = mutate.candidate_digest(tmp_path, ['candidate'])
    path.unlink()
    path.symlink_to('target')
    link = mutate.candidate_digest(tmp_path, ['candidate'])
    assert len({plain, executable, link}) == 3


@pytest.mark.parametrize('arguments', [[], ['mutants.py', '--tests', 'tests/test_calc.py']])
def test_mutate_requires_either_a_mutants_file_or_tests(arguments, capsys):
    """The two modes exclude each other, and one of them is required."""
    with pytest.raises(SystemExit) as raised:
        mutate.main(['--evidence', 'e', *arguments])
    assert raised.value.code == 2
    assert 'either a mutants file or --tests' in capsys.readouterr().err


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


@pytest.mark.parametrize('kind', ['absolute', 'climbing', 'symlink', 'below a symlink'])
def test_mutate_refuses_a_mutant_that_could_change_a_file_outside_the_copy(tmp_path, repo, capsys, monkeypatch, kind):
    """A mutant path that leads out of the copy stops the batch before the outside file is read or written."""
    # The copy lives at <tempdir>/mutate-*/tree, so three levels up from it is tmp_path, which owns the sentinel.
    (tmp_path / 'scratch').mkdir()
    monkeypatch.setattr(tempfile, 'tempdir', str(tmp_path / 'scratch'))
    outside = tmp_path / 'outside'
    outside.mkdir()
    sentinel = outside / 'calc.py'
    sentinel.write_text('def add(a, b):\n    return a + b\n', encoding='utf-8')
    if kind in ('symlink', 'below a symlink'):
        (repo / 'link').symlink_to(CALC if kind == 'symlink' else 'youtube_collector')
        git(repo, 'add', 'link')
        git(repo, 'commit', '-q', '-m', 'link')
    path = {
        'absolute': str(sentinel), 'climbing': '../../../outside/calc.py', 'symlink': 'link',
        'below a symlink': 'link/calc.py',
    }[kind]
    status, evidence = run_batch(tmp_path, repo, ('M1', 'adds', path, 'a + b', 'a - b', TESTS))
    assert status == 2
    assert f'{path} is' in capsys.readouterr().err
    assert sentinel.read_text(encoding='utf-8') == 'def add(a, b):\n    return a + b\n'
    assert 'a + b' in (repo / CALC).read_text(encoding='utf-8')
    assert not evidence.exists()


SWAP_TEST = '''from pathlib import Path
import shutil

SENTINEL = Path({sentinel!r})


def test_swap():
    """Replace the mutated file, or its directory, with a link out of the copy once the mutant is in place."""
    calc = Path('youtube_collector/calc.py')
    if 'a - b' in calc.read_text(encoding='utf-8'):
        if {parent!r}:
            shutil.move('youtube_collector', 'moved')
            Path('youtube_collector').symlink_to(SENTINEL.parent)
        else:
            calc.unlink()
            calc.symlink_to(SENTINEL)
        assert False, 'mutated'
'''


@pytest.mark.parametrize('parent', [False, True], ids=['file', 'parent'])
def test_mutate_restores_the_copy_without_following_a_link_that_a_test_put_in_place(tmp_path, repo, capsys, parent):
    """A test that swaps the mutated file or its directory for a link cannot steer the restore out of the copy."""
    sentinel = tmp_path / 'outside' / 'calc.py'
    sentinel.parent.mkdir()
    sentinel.write_text('outside', encoding='utf-8')
    probe = tmp_path / 'test_swap.py'
    probe.write_text(SWAP_TEST.format(sentinel=str(sentinel), parent=parent), encoding='utf-8')
    status, _ = run_batch(
        tmp_path, repo, ('M1', 'swaps', CALC, 'a + b', 'a - b', ['tests/test_swap.py']),
        extra=['--add', f'{probe}=tests/test_swap.py'],
    )
    assert sentinel.read_text(encoding='utf-8') == 'outside'
    assert status == (2 if parent else 0)
    if parent:
        assert 'cannot be restored' in capsys.readouterr().err


HARD_LINK_TEST = '''import os
from pathlib import Path

SENTINEL = Path({sentinel!r})


def test_link():
    """At the named stage, replace the file with a hard link to a file outside the copy; fail on the mutant."""
    calc = Path('youtube_collector/calc.py')
    content = calc.read_text(encoding='utf-8')
    mutated = 'a - b' in content
    if 'outside' not in content and mutated == ({stage!r} == 'mutant'):
        calc.unlink()
        os.link(SENTINEL, calc)
    assert not mutated
'''


@pytest.mark.parametrize('stage', ['baseline', 'mutant'])
def test_mutate_never_writes_through_a_hard_link_that_a_test_made(tmp_path, repo, monkeypatch, stage):
    """A hard link made before the mutant's write, or before the restore, carries neither write out of the copy."""
    # os.link needs the copy and the sentinel on one filesystem, so the copy goes below tmp_path.
    (tmp_path / 'scratch').mkdir()
    monkeypatch.setattr(tempfile, 'tempdir', str(tmp_path / 'scratch'))
    sentinel = tmp_path / 'outside.py'
    sentinel.write_text('def add(a, b):\n    return a + b  # outside\n', encoding='utf-8')
    probe = tmp_path / 'test_link.py'
    probe.write_text(HARD_LINK_TEST.format(sentinel=str(sentinel), stage=stage), encoding='utf-8')
    status, evidence = run_batch(
        tmp_path, repo, ('M1', 'links', CALC, 'a + b', 'a - b', ['tests/test_link.py']),
        extra=['--add', f'{probe}=tests/test_link.py'],
    )
    assert sentinel.read_text(encoding='utf-8') == 'def add(a, b):\n    return a + b  # outside\n'
    assert status == 0
    assert '[KILLED]' in evidence.read_text(encoding='utf-8')



def test_mutate_removes_its_new_file_when_it_cannot_replace_the_target(tmp_path):
    """A failed replacement leaves no stray temporary file in the copy and reports the error."""
    tree = tmp_path / 'tree'
    (tree / 'pkg' / 'mod.py').mkdir(parents=True)
    with pytest.raises(OSError):
        mutate.replace_text(tree, 'pkg/mod.py', 'text', 0o644)
    assert sorted(path.name for path in (tree / 'pkg').iterdir()) == ['mod.py']

MODE_TEST = '''import os
from pathlib import Path


def test_mode():
    """Require the file to stay executable, and swap it for a symlink while the first mutant is in place."""
    calc = Path('youtube_collector/calc.py')
    assert os.access(calc, os.X_OK)
    if 'b + a' in calc.read_text(encoding='utf-8'):
        content = calc.read_text(encoding='utf-8')
        Path('moved.py').write_text(content, encoding='utf-8')
        calc.unlink()
        calc.symlink_to('../moved.py')
'''


def test_mutate_restores_the_mode_of_a_file_that_a_test_replaced(tmp_path, repo):
    """After a test swaps an executable file for a link, the next mutant still starts from an executable file."""
    (repo / CALC).chmod(0o755)
    git(repo, 'add', CALC)
    git(repo, 'commit', '-q', '-m', 'executable')
    probe = tmp_path / 'test_mode.py'
    probe.write_text(MODE_TEST, encoding='utf-8')
    status, evidence = run_batch(
        tmp_path, repo, ('M1', 'equivalent', CALC, 'a + b', 'b + a', ['tests/test_mode.py']),
        ('M2', 'equivalent', CALC, 'return a + b', 'return (a + b)', ['tests/test_mode.py']),
        extra=['--add', f'{probe}=tests/test_mode.py'],
    )
    text = evidence.read_text(encoding='utf-8')
    assert status == 1
    assert 'M1 [SURVIVED]' in text
    assert 'M2 [SURVIVED]' in text


def test_mutate_refuses_a_failing_baseline(tmp_path, repo, capsys):
    """Kills mean nothing against a failing baseline."""
    (repo / CALC).write_text('def add(a, b):\n    return 0\n', encoding='utf-8')
    status, _ = run_batch(tmp_path, repo, ('M1', 'adds', CALC, 'return 0', 'return 1', TESTS))
    error = capsys.readouterr().err
    assert status == 2
    assert 'Baseline' in error
    assert 'advice: a selected test fails with no mutant' in error


@pytest.mark.parametrize(('run', 'reason'), [
    (mutate.Run(None, 'timed out after 5 s', None, ()), 'the run timed out'),
    (mutate.Run(0, '2 passed', None, ()), 'provenance was not verified'),
    (mutate.Run(4, 'no tests ran', 'seen', ()), 'pytest exited with status 4'),
])
def test_mutate_explains_a_baseline_that_is_not_evidence(tmp_path, repo, capsys, run, reason):
    """A baseline that timed out, lost its provenance or ran nothing stops the batch with the remedy."""
    with patch.object(mutate, 'run_tests', return_value=run):
        status, evidence = run_batch(tmp_path, repo, ('M1', 'adds', CALC, 'a + b', 'a - b', TESTS))
    assert status == 2
    assert f'advice: {reason}' in capsys.readouterr().err
    assert not evidence.exists()


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


def test_mutate_refuses_a_relative_symlink_that_leads_from_the_copy_back_to_the_original(tmp_path, repo):
    """Surplus ``..`` components stop at ``/`` in the repository, so only a check in the copy catches them."""
    (repo / 'link').symlink_to('../' * 64 + str(repo / CALC).lstrip('/'))
    assert (repo / 'link').resolve() == (repo / CALC).resolve()
    with pytest.raises(ValueError, match='out of the copy'):
        mutate.copy_candidate(repo, tmp_path / 'deeper/tree', ['link', CALC])


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


NEW = 'tests/new.txt'


def frozen(tmp_path, repo, monkeypatch):
    """Freeze a candidate that changes a file, adds one and deletes one; return its worktree."""
    gate_stubs(repo, monkeypatch, tmp_path)
    (repo / CALC).write_text('def add(a, b):\n    return b + a\n', encoding='utf-8')
    (repo / NEW).write_text('new', encoding='utf-8')
    (repo / '.gitignore').unlink()
    worktree = (tmp_path / 'wt').resolve()
    assert worktree_gates.main([str(worktree), CALC, NEW, '.gitignore', '--repo', str(repo)]) == 0
    return worktree


def commit_candidate(repo):
    """Commit the candidate that ``frozen`` made, as the reviewed content."""
    git(repo, 'add', CALC, NEW)
    git(repo, 'rm', '-q', '--cached', '.gitignore')
    git(repo, 'commit', '-q', '-m', 'candidate')


def test_worktree_gates_compare_accepts_unchanged_working_files(tmp_path, repo, monkeypatch, capsys):
    """Working files that still match the reviewed ones compare equal, and unrelated changes do not count."""
    worktree = frozen(tmp_path, repo, monkeypatch)
    (repo / 'tests/test_calc.py').write_text('unrelated work\n', encoding='utf-8')
    assert worktree_gates.main([str(worktree), '--compare', '--repo', str(repo)]) == 0
    assert 'differ' not in capsys.readouterr().err


@pytest.mark.parametrize('change', ['content', 'mode', 'deleted', 'restored', 'link'])
def test_worktree_gates_compare_reports_a_working_file_that_changed_after_the_freeze(
    tmp_path, repo, monkeypatch, capsys, change,
):
    """A reviewed file whose content, mode, kind or existence changed is named, and nothing is removed."""
    worktree = frozen(tmp_path, repo, monkeypatch)
    name = CALC
    if change == 'content':
        (repo / CALC).write_text('def add(a, b):\n    return 0\n', encoding='utf-8')
    elif change == 'mode':
        (repo / CALC).chmod(0o755)
    elif change == 'deleted':
        (repo / CALC).unlink()
    elif change == 'restored':
        name = '.gitignore'
        (repo / name).write_text('.env\n', encoding='utf-8')
    else:
        name = NEW
        (repo / name).unlink()
        (repo / name).symlink_to('test_calc.py')
    assert worktree_gates.main([str(worktree), '--compare', '--repo', str(repo)]) == 1
    error = capsys.readouterr().err
    assert "the repository's working files differ" in error
    assert f"['{name}']" in error
    assert (worktree / NEW).exists()



def test_worktree_gates_compare_reports_a_named_file_that_matched_head_at_the_freeze(
    tmp_path, repo, monkeypatch, capsys,
):
    """A named file without changes is still reviewed content, so a later edit to it is reported."""
    gate_stubs(repo, monkeypatch, tmp_path)
    worktree = (tmp_path / 'wt').resolve()
    assert worktree_gates.main([str(worktree), TESTS[0], '--repo', str(repo)]) == 0
    (repo / TESTS[0]).write_text('edited after the review\n', encoding='utf-8')
    assert worktree_gates.main([str(worktree), '--compare', '--repo', str(repo)]) == 1
    assert f"['{TESTS[0]}']" in capsys.readouterr().err

def test_worktree_gates_compare_refuses_a_reviewed_path_below_a_symlink(tmp_path, repo, monkeypatch, capsys):
    """A parent that became a symlink must not lead the comparison to read outside the repository."""
    worktree = frozen(tmp_path, repo, monkeypatch)
    (repo / 'tests').rename(repo / 'moved')
    (repo / 'tests').symlink_to(tmp_path)
    assert worktree_gates.main([str(worktree), '--compare', '--repo', str(repo)]) == 2
    assert 'below a symlink' in capsys.readouterr().err


def test_worktree_gates_remove_the_worktree_once_the_commit_holds_the_reviewed_content(tmp_path, repo, monkeypatch):
    """After the commit, which may hold other files too, the worktree and its registration are gone."""
    worktree = frozen(tmp_path, repo, monkeypatch)
    commit_candidate(repo)
    (repo / 'other.txt').write_text('a completion record', encoding='utf-8')
    git(repo, 'add', 'other.txt')
    git(repo, 'commit', '-q', '-m', 'more')
    assert worktree_gates.main([str(worktree), '--remove', '--repo', str(repo)]) == 0
    assert not worktree.exists()
    assert str(worktree) not in git(repo, 'worktree', 'list')


@pytest.mark.parametrize('state', ['uncommitted', 'changed', 'deletion uncommitted', 'new file uncommitted'])
def test_worktree_gates_keep_a_worktree_whose_reviewed_content_is_not_committed(
    tmp_path, repo, monkeypatch, capsys, state,
):
    """The forced removal would destroy the only copy of reviewed content, so it is refused and named."""
    worktree = frozen(tmp_path, repo, monkeypatch)
    name = CALC
    if state == 'changed':
        (repo / CALC).write_text('def add(a, b):\n    return a + b + 0\n', encoding='utf-8')
        commit_candidate(repo)
    elif state == 'deletion uncommitted':
        name = '.gitignore'
        git(repo, 'add', CALC, NEW)
        git(repo, 'commit', '-q', '-m', 'partial')
    elif state == 'new file uncommitted':
        name = NEW
        git(repo, 'add', CALC)
        git(repo, 'rm', '-q', '--cached', '.gitignore')
        git(repo, 'commit', '-q', '-m', 'partial')
    assert worktree_gates.main([str(worktree), '--remove', '--repo', str(repo)]) == 1
    error = capsys.readouterr().err
    assert 'the commit at HEAD differ' in error
    assert f"'{name}'" in error
    assert 'the worktree is kept' in error
    assert 'b + a' in (worktree / CALC).read_text(encoding='utf-8')
    assert (worktree / NEW).read_text(encoding='utf-8') == 'new'
    assert str(worktree) in git(repo, 'worktree', 'list')


@pytest.mark.parametrize('mode', ['--compare', '--remove'])
def test_worktree_gates_see_a_mode_change_that_git_is_configured_to_ignore(tmp_path, repo, monkeypatch, capsys, mode):
    """With core.filemode off, a reviewed mode change still counts: it is compared, and never removed unseen."""
    gate_stubs(repo, monkeypatch, tmp_path)
    git(repo, 'config', 'core.filemode', 'false')
    (repo / CALC).chmod(0o755)
    worktree = (tmp_path / 'wt').resolve()
    assert worktree_gates.main([str(worktree), CALC, '--repo', str(repo)]) == 0
    (repo / CALC).chmod(0o644)
    assert worktree_gates.main([str(worktree), mode, '--repo', str(repo)]) == 1
    assert f"['{CALC}']" in capsys.readouterr().err
    assert os.access(worktree / CALC, os.X_OK)


@pytest.mark.parametrize('mode', ['--compare', '--remove'])
def test_worktree_gates_skip_only_the_files_named_as_changed(tmp_path, repo, monkeypatch, capsys, mode):
    """A file named as changed may differ, as a task record does; any other difference still counts."""
    worktree = frozen(tmp_path, repo, monkeypatch)
    (repo / NEW).write_text('results added after the review', encoding='utf-8')
    (repo / CALC).write_text('def add(a, b):\n    return a + b + 0\n', encoding='utf-8')
    commit_candidate(repo)
    arguments = [str(worktree), mode, '--repo', str(repo), '--changed', NEW]
    assert worktree_gates.main(arguments) == 1
    error = capsys.readouterr().err
    assert f"['{CALC}']" in error
    assert worktree.exists()
    (repo / CALC).write_text('def add(a, b):\n    return b + a\n', encoding='utf-8')
    git(repo, 'commit', '-q', '-am', 'the reviewed content')
    assert worktree_gates.main(arguments) == 0
    assert worktree.exists() == (mode == '--compare')


def test_worktree_gates_remove_leaves_the_worktree_index_alone(tmp_path, repo, monkeypatch):
    """Reading the reviewed content stages nothing in the worktree."""
    worktree = frozen(tmp_path, repo, monkeypatch)
    before = git(worktree, 'status', '--porcelain')
    assert worktree_gates.main([str(worktree), '--remove', '--repo', str(repo)]) == 1
    assert git(worktree, 'status', '--porcelain') == before
    assert '?? tests/new.txt' in before


@pytest.mark.parametrize('mode', ['--compare', '--remove'])
@pytest.mark.parametrize('change', ['restored', 'edited', 'deleted'])
def test_worktree_gates_refuse_a_frozen_worktree_that_changed_after_the_freeze(
    tmp_path, repo, monkeypatch, capsys, mode, change,
):
    """A reviewed file changed in the worktree, even back to HEAD's content, is named instead of compared away."""
    worktree = frozen(tmp_path, repo, monkeypatch)
    commit_candidate(repo)
    if change == 'restored':
        git(worktree, 'checkout', '--', CALC)
    elif change == 'edited':
        (worktree / CALC).write_text('def add(a, b):\n    return 0\n', encoding='utf-8')
    else:
        (worktree / CALC).unlink()
    assert worktree_gates.main([str(worktree), mode, '--repo', str(repo)]) == 2
    assert f"changed after the freeze in ['{CALC}']" in capsys.readouterr().err
    assert str(worktree) in git(repo, 'worktree', 'list')


def test_worktree_gates_keep_a_worktree_whose_index_stages_a_change(tmp_path, repo, monkeypatch, capsys):
    """A blob staged in the worktree exists nowhere else, so the forced removal that would discard it is refused."""
    worktree = frozen(tmp_path, repo, monkeypatch)
    commit_candidate(repo)
    reviewed = (worktree / CALC).read_text(encoding='utf-8')
    (worktree / CALC).write_text('staged only\n', encoding='utf-8')
    git(worktree, 'add', CALC)
    (worktree / CALC).write_text(reviewed, encoding='utf-8')
    assert worktree_gates.main([str(worktree), '--remove', '--repo', str(repo)]) == 2
    assert f"the worktree index stages changes in ['{CALC}']" in capsys.readouterr().err
    assert git(worktree, 'show', f':{CALC}') == 'staged only\n'


@pytest.mark.parametrize('mode', ['--compare', '--remove'])
def test_worktree_gates_refuse_a_worktree_without_a_freeze_record(tmp_path, repo, capsys, mode):
    """A worktree made by hand has no record of the reviewed paths, so neither mode trusts it."""
    worktree = (tmp_path / 'wt').resolve()
    git(repo, 'worktree', 'add', '-q', '--detach', str(worktree), 'HEAD')
    assert worktree_gates.main([str(worktree), mode, '--repo', str(repo)]) == 2
    assert 'has no freeze record' in capsys.readouterr().err
    assert worktree.exists()


@pytest.mark.parametrize('content', [
    '[]', '{"files": {}}', '{"head": "0"}', 'not json', '{"head": "%(head)s", "files": null}',
])
def test_worktree_gates_refuse_a_freeze_record_they_cannot_read(tmp_path, repo, monkeypatch, capsys, content):
    """A record from another version or a damaged one is a clean refusal, and the worktree is kept."""
    worktree = frozen(tmp_path, repo, monkeypatch)
    head = git(worktree, 'rev-parse', 'HEAD').strip()
    (Path(git(worktree, 'rev-parse', '--absolute-git-dir').strip()) / 'worktree-gates.json').write_text(
        content % {'head': head}, encoding='utf-8',
    )
    assert worktree_gates.main([str(worktree), '--remove', '--repo', str(repo)]) == 2
    assert 'worktree_gates:' in capsys.readouterr().err
    assert worktree.exists()


@pytest.mark.parametrize('mode', ['--compare', '--remove'])
@pytest.mark.parametrize('change', ['commit', 'unnamed file'])
def test_worktree_gates_refuse_a_worktree_changed_outside_its_named_files(
    tmp_path, repo, monkeypatch, capsys, mode, change,
):
    """A commit in the worktree or a change to an unnamed path is outside the review, so neither mode accepts it."""
    worktree = frozen(tmp_path, repo, monkeypatch)
    commit_candidate(repo)
    if change == 'commit':
        git(worktree, 'commit', '-q', '--allow-empty', '-m', 'only here')
        expected = "HEAD moved"
    else:
        for root in (repo, worktree):
            (root / 'tests/extra.txt').write_text('added after the freeze', encoding='utf-8')
        expected = "changed after the freeze in ['tests/extra.txt']"
    assert worktree_gates.main([str(worktree), mode, '--repo', str(repo)]) == 2
    assert expected in capsys.readouterr().err
    assert str(worktree) in git(repo, 'worktree', 'list')


def test_worktree_gates_report_a_path_that_is_not_a_worktree(tmp_path, repo, capsys):
    """A directory that git does not know is a clean error, and it is kept."""
    (tmp_path / 'plain').mkdir()
    (tmp_path / 'plain/keep').touch()
    assert worktree_gates.main([str(tmp_path / 'plain'), '--remove', '--repo', str(repo)]) == 2
    assert 'worktree_gates:' in capsys.readouterr().err
    assert (tmp_path / 'plain/keep').exists()


def test_worktree_gates_refuse_to_remove_the_main_working_tree(repo, capsys):
    """The repository itself is never removed, even when it is clean."""
    assert worktree_gates.main([str(repo), '--remove', '--repo', str(repo)]) == 2
    assert 'worktree_gates:' in capsys.readouterr().err
    assert (repo / CALC).exists()


@pytest.mark.parametrize('arguments', [['wt'], ['wt', 'file', '--remove'], ['wt', 'file', '--compare']])
def test_worktree_gates_require_files_only_to_freeze(arguments, capsys):
    """Freezing needs the candidate's files; comparing and removing take none."""
    with pytest.raises(SystemExit) as raised:
        worktree_gates.main(arguments)
    assert raised.value.code == 2
    assert 'name the files' in capsys.readouterr().err


@pytest.mark.parametrize('name', ['mutate', 'worktree_gates'])
def test_scripts_run_as_programs(name, monkeypatch, capsys):
    """Each script's entry point runs its command line; --help exits cleanly."""
    monkeypatch.setattr(sys, 'argv', [name, '--help'])
    with pytest.raises(SystemExit) as raised:
        runpy.run_path(str(REFERENCES / f'{name}.py'), run_name='__main__')
    assert raised.value.code == 0
    assert 'usage' in capsys.readouterr().out
