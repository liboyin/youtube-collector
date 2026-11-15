"""Exercise the documentation check with owned Git repositories."""

import importlib.util
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

# AGENTS permits file-based loading for standalone scripts outside the package.
SCRIPT = Path(__file__).resolve().parents[2] / 'check_docs.py'
SPEC = importlib.util.spec_from_file_location('check_docs', SCRIPT)
assert SPEC is not None and SPEC.loader is not None
testee = importlib.util.module_from_spec(SPEC)
sys.modules['check_docs'] = testee  # dataclasses resolve annotations through the module table
SPEC.loader.exec_module(testee)

LONG = ' '.join(['word'] * 36) + '.'
ITEM_LONG = '- ' + ' '.join(['word'] * 29) + '.'
PARTIAL = ['DESIGN.md', 'project-review-2026-11-10.md', 'tasks/open/T1.md', 'tasks/closed/T2.md']


class Repo:
    """An owned repository whose files the tests write, commit and check."""

    def __init__(self, root):
        """Create the repository with one commit, so that ``HEAD`` exists."""
        self.root = root
        root.mkdir()
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Docs test')
        self.git('config', 'user.email', 'test@example.invalid')
        self.write('README.md', '# Project\n')
        self.commit()

    def git(self, *arguments):
        """Run git in this repository and return its output."""
        return subprocess.run(['git', *arguments], cwd=self.root, check=True, capture_output=True, text=True).stdout

    def write(self, name, text):
        """Write a file, creating its directories."""
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')

    def commit(self):
        """Commit every file in the repository."""
        self.git('add', '.')
        self.git('commit', '-q', '-m', 'state')

    def check(self, capsys):
        """Run the check through its command line; return the status and the printed problem lines."""
        status = testee.main(['--repo', str(self.root)])
        lines = capsys.readouterr().out.splitlines()
        return status, lines[:-1]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """Provide an owned repository under a Git configuration that reads nothing from the host."""
    for name in tuple(os.environ):
        if name.startswith('GIT_'):
            monkeypatch.delenv(name)
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(home))
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', os.devnull)
    # Git only honors this boundary when discovery starts below it.
    monkeypatch.setenv('GIT_CEILING_DIRECTORIES', str(tmp_path))
    return Repo(tmp_path / 'repo')


def test_clean_documentation_passes(repo, capsys):
    """Short sentences, resolving links and exempt regions give no problem and status zero."""
    repo.write('docs/guide.md', '# Guide\n\n## Second part\n\nA short paragraph. It has two sentences.\n')
    repo.write('README.md', (
        '---\ndescription: a; b and/or c ' + LONG + '\n---\n'
        '# Project\n\n'
        'See the [guide](docs/guide.md#second-part), the [folder](docs/) and a [site](https://example.invalid/a;b).\n\n'
        '- Run `a/b; c` first.\n'
        '1. Then read [this file](#project).\n\n'
        '| Cell | ' + LONG + '; and/or |\n\n'
        '> ' + LONG + '; and/or\n\n'
        '```sh\n' + LONG + '; and/or [broken](missing.md)\n```\n\n'
        '## A heading with a semicolon; and/or\n'
    ))
    assert repo.check(capsys) == (0, [])


@pytest.mark.parametrize(('link', 'message'), [
    ('missing.md', 'does not exist'),
    ('docs/guide.md#absent', 'names no heading in its target'),
    ('#absent', 'names no heading in its target'),
    ('../outside.md', 'is not a relative path in the repository'),
    ('/tmp/scratch/record.md', 'is not a relative path in the repository'),
])
def test_a_link_that_does_not_resolve_is_reported(repo, capsys, link, message):
    """A missing file, a missing heading and a path that leaves the repository each fail the check."""
    repo.write('docs/guide.md', '# Guide\n')
    repo.write('README.md', f'# Project\n\nSee [it]({link}).\n')
    status, lines = repo.check(capsys)
    assert status == 1
    assert lines == [f'README.md:3: link: {link} {message}']


@pytest.mark.parametrize('link', [
    '[it](missing.md "A title")', "[it](missing.md 'A title')", '[it](missing.md (A title))', '[it](<missing.md>)',
    '[it]( missing.md )', '![image](missing.md)',
])
def test_a_broken_link_is_reported_in_each_inline_form(repo, capsys, link):
    """A title, angle brackets, padding and an image mark do not hide a destination from the check."""
    repo.write('README.md', f'# Project\n\nSee {link}.\n')
    status, lines = repo.check(capsys)
    assert status == 1
    assert lines == ['README.md:3: link: missing.md does not exist']


def test_valid_link_forms_pass(repo, capsys):
    """Titles, angle brackets and balanced parentheses in a destination resolve to the right file."""
    repo.write('docs/guide_(v1).md', '# Guide\n\n## Part\n')
    repo.write('docs/with space.md', '# Space\n')
    repo.write('README.md', (
        '# Project\n\n'
        '- [a](docs/guide_(v1).md)\n- [b](docs/guide_(v1).md#part "A title")\n- [c](<docs/with space.md>)\n'
        '- [d](docs/with%20space.md)\n- [e][guide] and [guide][] and [Guide][GUIDE]\n\n'
        '[guide]: docs/guide_(v1).md#part "A title"\n'
    ))
    assert repo.check(capsys) == (0, [])


def test_reference_links_are_checked(repo, capsys):
    """A definition with a broken destination and a reference without a definition are both reported."""
    repo.write('README.md', (
        '# Project\n\nSee [the guide][guide] and [the rules][rules] and [rules][].\n\n[guide]: missing.md\n'
    ))
    status, lines = repo.check(capsys)
    assert status == 1
    assert lines == [
        'README.md:3: link: [rules] has no link definition',
        'README.md:3: link: [rules] has no link definition',
        'README.md:5: link: missing.md does not exist',
    ]


def test_a_link_definition_is_not_prose(repo, capsys):
    """A definition line is exempt from the style rules and does not continue a paragraph."""
    repo.write('docs/a;b.md', '# A\n')
    repo.write('README.md', '# Project\n\nSee [it][a].\n[a]: docs/a;b.md\n')
    assert repo.check(capsys) == (0, [])


def test_links_in_tables_headings_and_quotes_are_checked(repo, capsys):
    """Regions that are exempt from the style rules still hold real links."""
    repo.write('README.md', '# Project\n\n| [a](missing-a.md) |\n\n> [b](missing-b.md)\n\n## [c](missing-c.md)\n')
    status, lines = repo.check(capsys)
    assert status == 1
    assert [line.split(': ')[2] for line in lines] == [
        'missing-a.md does not exist', 'missing-b.md does not exist', 'missing-c.md does not exist',
    ]


def test_anchors_follow_the_derivation_of_the_host(repo, capsys):
    """Punctuation is dropped, links in headings keep their text, and a repeated heading gets a number."""
    repo.write('docs/guide.md', (
        '# Guide\n\n## 5.12 Restart, recovery (main agent)\n\n## The [task](guide.md) rules\n\n## Twice\n\n## Twice\n\n'
        '```\n## Not a heading\n```\n'
    ))
    links = ['512-restart-recovery-main-agent', 'the-task-rules', 'twice', 'twice-1', 'not-a-heading']
    repo.write('README.md', '# Project\n\n' + ''.join(f'- [x](docs/guide.md#{link})\n' for link in links))
    status, lines = repo.check(capsys)
    assert status == 1
    assert lines == ['README.md:7: link: docs/guide.md#not-a-heading names no heading in its target']


@pytest.mark.parametrize(('line', 'rule'), [
    (LONG, 'sentence'),
    (ITEM_LONG, 'sentence'),
    ('One clause; another clause.', 'semicolon'),
    ('Record the model/effort pair.', 'slash'),
    ('A. B. C. D. E. F. G. H.', 'paragraph'),
])
def test_each_style_rule_is_reported(repo, capsys, line, rule):
    """A sentence over its limit, a semicolon, a slash and a long paragraph each fail the check."""
    repo.write('README.md', f'# Project\n\n{line}\n')
    status, lines = repo.check(capsys)
    assert status == 1
    assert [found.split(': ')[1] for found in lines] == [rule]
    assert lines[0].startswith('README.md:3: ')


@pytest.mark.parametrize('line', [
    ' '.join(['word'] * 35) + '.',
    '- ' + ' '.join(['word'] * 28) + '.',
    'A. B. C. D. E. F. G.',
    'Use HTTP/1.1 at https://example.invalid/a/b, or see docs/guide.md and tasks/open/.',
    'The [review/skill](README.md) link text is checked, so this line names `a/b` only in code.',
])
def test_text_at_the_limits_passes(repo, capsys, line):
    """The limits are inclusive, and paths, URLs and code are not alternatives joined by a slash."""
    repo.write('README.md', f'# Project\n\n{line}\n')
    status, lines = repo.check(capsys)
    expected = ['slash'] if 'review/skill' in line else []
    assert [found.split(': ')[1] for found in lines] == expected
    assert status == (1 if expected else 0)


def test_a_colon_does_not_end_a_sentence(repo, capsys):
    """Forty words around a colon are one sentence, not two short ones."""
    half = ' '.join(['word'] * 20)
    repo.write('README.md', f'# Project\n\n{half}: {half}.\n')
    status, lines = repo.check(capsys)
    assert status == 1
    assert [line.split(': ')[1] for line in lines] == ['sentence']
    assert '40 words' in lines[0]


def test_a_sentence_ends_inside_closing_markup(repo, capsys):
    """A bold label, a quotation and a bracket can close after the full stop that ends a sentence."""
    twenty = ' '.join(['word'] * 20)
    repo.write('README.md', (
        f'# Project\n\n**{twenty}.** {twenty}.\n\n"{twenty}?" {twenty}.\n\n({twenty}!) _{twenty}._ {twenty}.\n'
    ))
    assert repo.check(capsys) == (0, [])


@pytest.mark.parametrize(('block', 'clean'), [
    ('~~~~\n```\nBad; text.\n```\nBad; text.\n~~~~\n', True),
    ('```\n~~~\nBad; text.\n~~~\nBad; text.\n```\n', True),
    ('````\n```\nBad; text.\n````\n', True),
    ('```\nBad; text.\n``` sh\nBad; text.\n```\n', True),
    ('```\ncode\n```\nBad; text.\n', False),
    ('~~~\ncode\n~~~~\nBad; text.\n', False),
])
def test_a_code_block_ends_only_at_its_matching_fence(repo, capsys, block, clean):
    """A fence of another character, a shorter fence or a fence with an info string stays inside the block."""
    repo.write('README.md', f'# Project\n\n{block}')
    status, lines = repo.check(capsys)
    assert (status, [line.split(': ')[1] for line in lines]) == ((0, []) if clean else (1, ['semicolon']))


def test_a_list_item_has_the_instruction_limit_and_prose_the_description_limit(repo, capsys):
    """Thirty words pass as prose and fail as a list item."""
    thirty = ' '.join(['word'] * 30) + '.'
    repo.write('README.md', f'# Project\n\n{thirty}\n\n- {thirty}\n')
    status, lines = repo.check(capsys)
    assert status == 1
    assert [line.split(': ')[0] for line in lines] == ['README.md:5']
    assert 'the limit is 28' in lines[0]


def test_a_paragraph_on_two_source_lines_is_reported(repo, capsys):
    """One source line holds one paragraph; a list after a paragraph is not a continuation."""
    repo.write('README.md', '# Project\n\nFirst line.\nSecond line.\n- An item.\n- Another item.\n')
    status, lines = repo.check(capsys)
    assert status == 1
    assert lines == ['README.md:4: line: a paragraph continues on a second source line']


@pytest.mark.parametrize('name', PARTIAL)
def test_only_changed_lines_are_checked_in_files_that_are_not_rewritten(repo, capsys, name):
    """Committed dense text stays unreported; a line that the working tree adds or changes is checked."""
    title = f'# {Path(name).stem}: a title'
    repo.write(name, f'{title}\n\n{LONG}\n\nOld; text.\n')
    repo.commit()
    assert repo.check(capsys) == (0, [])
    repo.write(name, f'{title}\n\n{LONG}\n\nOld; text.\n\nNew; text.\n')
    status, lines = repo.check(capsys)
    assert status == 1
    assert [line.split(': ')[:2] for line in lines] == [[f'{name}:7', 'semicolon']]


def test_a_staged_change_counts_as_changed(repo, capsys):
    """The comparison is against HEAD, so a staged line is checked like an unstaged one."""
    repo.write('DESIGN.md', '# Design\n\nOld; text.\n')
    repo.commit()
    repo.write('DESIGN.md', '# Design\n\nOld; text.\n\nNew; text.\n')
    repo.git('add', 'DESIGN.md')
    status, lines = repo.check(capsys)
    assert status == 1
    assert [line.split(': ')[0] for line in lines] == ['DESIGN.md:5']


def test_a_moved_task_file_keeps_its_old_lines_unchecked(repo, capsys):
    """A record that git mv moved from open to closed is checked only where it changed."""
    old = '# T1: a title\n\n' + ''.join(f'Old; text {number}.\n\n' for number in range(8))
    repo.write('tasks/open/T1.md', old)
    repo.commit()
    (repo.root / 'tasks/closed').mkdir()
    repo.git('mv', 'tasks/open/T1.md', 'tasks/closed/T1.md')
    repo.write('tasks/closed/T1.md', old + 'New; text.\n')
    status, lines = repo.check(capsys)
    assert status == 1
    assert [line.split(': ')[0] for line in lines] == ['tasks/closed/T1.md:19']


def test_an_untracked_file_is_checked_as_a_whole(repo, capsys):
    """A new record has no committed lines, so every line of it is in scope."""
    repo.write('tasks/closed/T3.md', '# T3: a title\n\nNew; text.\n\nMore; text.\n')
    status, lines = repo.check(capsys)
    assert status == 1
    assert [line.split(': ')[0] for line in lines] == ['tasks/closed/T3.md:3', 'tasks/closed/T3.md:5']


def test_ignored_and_deleted_files_are_not_checked(repo, capsys):
    """An ignored file is never read, and a file deleted from the working tree is not a problem."""
    repo.write('.gitignore', 'ignored.md\n')
    repo.write('gone.md', '# Gone\n')
    repo.commit()
    repo.write('ignored.md', 'Bad; text with a [link](missing.md).\n')
    (repo.root / 'gone.md').unlink()
    assert repo.check(capsys) == (0, [])


def tasks(repo, index):
    """Write a task index with the given lines, one open task and one closed task."""
    repo.write('tasks/TODO.md', '# TODO\n\n## Open tasks\n\n' + index)
    repo.write('tasks/open/T1.md', '# T1: an open task\n')
    repo.write('tasks/closed/T2.md', '# T2: a closed task\n')


def test_consistent_task_files_pass(repo, capsys):
    """One index line for each open task, distinct IDs and matching titles give no problem."""
    tasks(repo, '- [T1](open/T1.md) — Low. An open task.\n')
    assert repo.check(capsys) == (0, [])


def test_an_open_task_without_an_index_line_is_reported(repo, capsys):
    """Every open task must appear in the index."""
    tasks(repo, '')
    status, lines = repo.check(capsys)
    assert status == 1
    assert lines == ['tasks/open/T1.md:1: tasks: T1 has no line in the index of tasks/TODO.md']


def test_an_index_line_without_a_file_is_reported_as_a_broken_link(repo, capsys):
    """A closed task that kept its index line points at a file that no longer exists."""
    tasks(repo, '- [T1](open/T1.md) — Low. An open task.\n- [T2](open/T2.md) — Low. Closed.\n')
    status, lines = repo.check(capsys)
    assert status == 1
    assert lines == ['tasks/TODO.md:6: link: open/T2.md does not exist']


def test_index_lines_must_be_unique_and_labelled_with_their_task(repo, capsys):
    """A second line for one task and a label that names another task are both reported."""
    tasks(repo, '- [T1](open/T1.md) — Low.\n- [T9](open/T1.md) — Low.\n')
    status, lines = repo.check(capsys)
    assert status == 1
    assert lines == [
        'tasks/TODO.md:6: tasks: T1 has more than one index line',
        'tasks/TODO.md:6: tasks: the label T9 links to T1',
    ]


def test_an_id_cannot_be_open_and_closed(repo, capsys):
    """A task that was copied instead of moved is reported."""
    tasks(repo, '- [T1](open/T1.md) — Low.\n')
    repo.write('tasks/closed/T1.md', '# T1: an open task\n')
    status, lines = repo.check(capsys)
    assert status == 1
    assert lines == ['tasks/open/T1.md:1: tasks: T1 is both open and closed']


@pytest.mark.parametrize('name', ['tasks/open/T1.md', 'tasks/closed/T2.md'])
def test_a_task_title_must_start_with_its_id(repo, capsys, name):
    """The ID in the title matches the file name, for open and for closed tasks."""
    tasks(repo, '- [T1](open/T1.md) — Low.\n')
    repo.write(name, '# T7: another task\n')
    status, lines = repo.check(capsys)
    assert status == 1
    assert lines == [f'{name}:1: tasks: the title does not start with "# {Path(name).stem}:"']


def test_task_files_without_an_index_are_not_checked(repo, capsys):
    """A repository without tasks/TODO.md has no task rules."""
    repo.write('tasks/open/T1.md', '# Something else\n')
    assert repo.check(capsys) == (0, [])


def test_problems_are_sorted_and_counted(repo, capsys):
    """The output lists problems by file and line, and its last line gives the totals."""
    repo.write('b.md', '# B\n\nBad; text.\n')
    repo.write('a.md', '# A\n\nSee [it](missing.md).\n\nBad; text.\n')
    status = testee.main(['--repo', str(repo.root)])
    lines = capsys.readouterr().out.splitlines()
    assert status == 1
    assert [line.split(': ')[0] for line in lines[:-1]] == ['a.md:3', 'a.md:5', 'b.md:3']
    assert lines[-1] == 'check_docs: files checked: 3, problems: 3'


def test_the_check_runs_from_a_subdirectory(repo, capsys, monkeypatch):
    """The repository root is found from the working directory, and paths are relative to the root."""
    repo.write('docs/guide.md', '# Guide\n\nBad; text.\n')
    monkeypatch.chdir(repo.root / 'docs')
    status = testee.main([])
    assert status == 1
    assert capsys.readouterr().out.splitlines()[0].startswith('docs/guide.md:3: semicolon')


def test_a_directory_that_is_no_repository_cannot_be_checked(tmp_path, repo, capsys):
    """Without a repository the check reports why it cannot run, with status two."""
    plain = tmp_path / 'plain'
    plain.mkdir()
    assert testee.main(['--repo', str(plain)]) == 2
    assert 'check_docs: cannot check the documentation' in capsys.readouterr().err


def test_a_repository_without_a_commit_cannot_be_checked(tmp_path, repo, capsys):
    """Changed lines are defined against HEAD, so a repository without one is reported."""
    empty = tmp_path / 'empty'
    empty.mkdir()
    subprocess.run(['git', 'init', '-q'], cwd=empty, check=True)
    assert testee.main(['--repo', str(empty)]) == 2
    assert 'check_docs: cannot check the documentation' in capsys.readouterr().err


def test_an_unreadable_file_cannot_be_checked(repo, capsys):
    """A Markdown file that is not text stops the check with status two instead of a traceback."""
    (repo.root / 'binary.md').write_bytes(b'\xff\xfe\x00')
    assert testee.main(['--repo', str(repo.root)]) == 2
    assert 'check_docs: cannot check the documentation: binary.md is not UTF-8 text' in capsys.readouterr().err


def test_the_script_runs_as_a_program(monkeypatch, capsys):
    """The entry point runs the command line; --help exits cleanly."""
    monkeypatch.setattr(sys, 'argv', ['check_docs', '--help'])
    with pytest.raises(SystemExit) as raised:
        runpy.run_path(str(SCRIPT), run_name='__main__')
    assert raised.value.code == 0
    assert 'usage' in capsys.readouterr().out
