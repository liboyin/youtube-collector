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


@pytest.mark.parametrize('arguments', [[], ['--references', 'README.md']])
def test_a_source_below_an_external_symlink_is_rejected_before_reading(repo, capsys, monkeypatch, arguments):
    """Both modes reject a tracked source whose parent escapes, without reading the external fixture."""
    repo.write('docs/guide.md', '# Guide\n')
    repo.commit()
    outside = repo.root.parent / 'outside'
    (repo.root / 'docs').rename(outside)
    (repo.root / 'docs').symlink_to(outside, target_is_directory=True)
    (outside / 'guide.md').write_bytes(b'\xff\xfe owned external fixture')
    reads = []
    original = Path.read_text

    def record_read(path, *args, **kwargs):
        """Observe content reads without blocking them, including reads by a broken containment check."""
        reads.append(path.resolve())
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', record_read)
    status = testee.main(['--repo', str(repo.root), *arguments])
    assert all(path.is_relative_to(repo.root) for path in reads)
    assert status == 2
    captured = capsys.readouterr()
    assert captured.out == ''
    assert 'docs/guide.md resolves outside the repository' in captured.err


@pytest.mark.parametrize('arguments', [[], ['--references', 'README.md']])
def test_a_source_below_an_internal_symlink_remains_readable(repo, capsys, arguments):
    """An internal parent alias still supplies the tracked source to both modes."""
    repo.write('docs/guide.md', '# Guide\n\n[project](../README.md)\n')
    repo.commit()
    (repo.root / 'docs').rename(repo.root / 'moved')
    (repo.root / 'docs').symlink_to('moved', target_is_directory=True)
    assert testee.main(['--repo', str(repo.root), *arguments]) == 0
    output = capsys.readouterr().out
    assert ('docs/guide.md:3' in output) if arguments else ('problems: 0' in output)


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


@pytest.mark.parametrize(('name', 'encoded'), [('part#one.md', 'part%23one.md'), ('part%23one.md', 'part%2523one.md')])
@pytest.mark.parametrize('fragment', ['', '#%C3%BCber-part'])
def test_encoded_filename_hashes_are_not_fragment_separators(repo, capsys, name, encoded, fragment):
    """Decode each component once, keeping an encoded hash in the filename and a real fragment separate."""
    repo.write(name, '# Guide\n\n## Über part\n')
    repo.write('README.md', f'# Project\n\n[guide]({encoded}{fragment})\n')
    assert repo.check(capsys) == (0, [])


def test_an_encoded_filename_still_checks_its_heading(repo, capsys):
    """A valid encoded filename does not hide a missing heading from the default check."""
    repo.write('part#one.md', '# Guide\n')
    repo.write('README.md', '# Project\n\n[guide](part%23one.md#missing)\n')
    assert repo.check(capsys) == (1, [
        'README.md:3: link: part%23one.md#missing names no heading in its target'])


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


def report(repo, capsys, target):
    """Run the reference report; return the status and the lines of its output."""
    status = testee.main(['--repo', str(repo.root), '--references', target])
    return status, capsys.readouterr().out.splitlines()


def test_a_file_query_finds_links_to_the_file_and_to_its_headings(repo, capsys):
    """Direct links, heading links and a link from the file to itself match; other targets do not."""
    repo.write('guide.md', '# Guide\n\n## Part\n\nSee [the part](#part).\n')
    repo.write('other.md', '# Other\n')
    repo.write('README.md', '# Project\n\n[a](guide.md) and [b](other.md).\n\nText.\n\n[c](guide.md#part)\n')
    assert report(repo, capsys, 'guide.md') == (0, [
        'References to guide.md:', 'README.md:3', 'README.md:7', 'guide.md:5', '', '3 references found.'])


def test_a_heading_query_finds_only_links_to_that_heading(repo, capsys):
    """Links to another heading of the file, or to the file without a fragment, do not match."""
    repo.write('guide.md', '# Guide\n\n## Part\n\n## Rest\n')
    repo.write('README.md', '# Project\n\n[a](guide.md)\n\n[b](guide.md#part)\n\n[c](guide.md#rest)\n')
    assert report(repo, capsys, 'guide.md#part') == (0, [
        'References to guide.md#part:', 'README.md:5', '', '1 reference found.'])


def test_relative_and_encoded_links_name_the_same_target(repo, capsys):
    """Links from other directories, redundant path segments and URL encoding all resolve to the query."""
    repo.write('docs/my guide.md', '# Guide\n\n## Über part\n')
    repo.write('README.md', '# Project\n\n[a](docs/my%20guide.md#%C3%BCber-part)\n\n[b](<docs/my guide.md>)\n')
    repo.write('docs/deep/note.md', '# Note\n\n[c](../my%20guide.md#über-part)\n\n[d](../../docs/./my%20guide.md)\n')
    assert report(repo, capsys, 'docs/my guide.md')[1][1:-2] == [
        'README.md:3', 'README.md:5', 'docs/deep/note.md:3', 'docs/deep/note.md:5']
    assert report(repo, capsys, 'docs/../docs/my guide.md#über-part')[1] == [
        'References to docs/my guide.md#über-part:', 'README.md:3', 'docs/deep/note.md:3', '', '2 references found.']


@pytest.mark.parametrize('fragment', ['', '#über-part'])
def test_the_report_matches_encoded_hash_filenames(repo, capsys, fragment):
    """File and heading reports match encoded hashes without treating them as fragment separators."""
    repo.write('part#one.md', '# Guide\n\n## Über part\n')
    # The query syntax reserves a literal # for the fragment; an internal alias names the same file.
    (repo.root / 'alias.md').symlink_to('part#one.md')
    repo.write('README.md', '# Project\n\n[file](part%23one.md)\n\n[part](part%23one.md#%C3%BCber-part)\n')
    expected = ['README.md:5'] if fragment else ['README.md:3', 'README.md:5']
    status, lines = report(repo, capsys, f'alias.md{fragment}')
    assert status == 0
    assert lines[1:-2] == expected


def test_a_reference_link_is_reported_where_it_is_used(repo, capsys):
    """A use of a defined label matches at its own line; the definition and an unused one do not."""
    repo.write('guide.md', '# Guide\n')
    repo.write('README.md', (
        '# Project\n\nSee [the guide][g] and [G][].\n\nAlso [the guide][unknown].\n\n'
        '[g]: guide.md\n[unused]: guide.md\n'))
    assert report(repo, capsys, 'guide.md')[1][1:-2] == ['README.md:3']


def test_code_gives_no_reference(repo, capsys):
    """A link in inline code or in a code block is not a reference, and no match is a success."""
    repo.write('guide.md', '# Guide\n')
    repo.write('README.md', '# Project\n\nWrite `[a](guide.md)`.\n\n```\n[b](guide.md)\n```\n')
    assert report(repo, capsys, 'guide.md') == (0, ['References to guide.md:', '', '0 references found.'])


def test_a_line_is_reported_once_and_the_report_is_sorted(repo, capsys):
    """Two links on one line give one result, and results are in order of path, then line number."""
    repo.write('guide.md', '# Guide\n')
    repo.write('b.md', '# B\n\n[x](guide.md) and [y](guide.md#guide)\n')
    lines = ['# A', ''] + ['Text.', ''] * 4 + ['[x](guide.md)', '', '[y](guide.md)', '']
    repo.write('a.md', '\n'.join(lines))
    first = report(repo, capsys, 'guide.md')
    assert first[1][1:-2] == ['a.md:11', 'a.md:13', 'b.md:3']
    assert report(repo, capsys, 'guide.md') == first


def test_a_directory_query_finds_links_to_that_directory_only(repo, capsys):
    """A directory is a valid target; a link to a file in it is not a link to the directory."""
    repo.write('docs/guide.md', '# Guide\n')
    repo.write('README.md', '# Project\n\n[d](docs/)\n\n[g](docs/guide.md)\n')
    assert report(repo, capsys, 'docs')[1] == ['References to docs:', 'README.md:3', '', '1 reference found.']


@pytest.mark.parametrize(('target', 'message'), [
    ('missing.md', 'missing.md names no file or directory in the repository'),
    ('../outside.md', '../outside.md names no file or directory in the repository'),
    ('#part', '#part names no file or directory in the repository'),
    ('guide.md#missing', 'guide.md#missing names no heading in guide.md'),
    ('docs#part', 'docs#part names no heading in docs'),
    ('data.txt#part', 'data.txt#part names no heading in data.txt'),
])
def test_an_invalid_query_is_an_error(repo, capsys, target, message):
    """A query without an existing target or heading gives status two and names the reason."""
    repo.write('guide.md', '# Guide\n\n## Part\n')
    repo.write('docs/note.md', '# Note\n')
    repo.write('data.txt', '# Part\n')
    (repo.root.parent / 'outside.md').write_text('# Outside\n', encoding='utf-8')
    assert testee.main(['--repo', str(repo.root), '--references', target]) == 2
    captured = capsys.readouterr()
    assert captured.err.strip() == f'check_docs: cannot check the documentation: {message}'
    assert captured.out == ''


def test_an_absolute_query_is_an_error(repo, capsys):
    """The query is a path from the repository root, so an absolute path to a file in it is rejected."""
    repo.write('guide.md', '# Guide\n')
    assert testee.main(['--repo', str(repo.root), '--references', str(repo.root / 'guide.md')]) == 2
    assert 'names no file or directory in the repository' in capsys.readouterr().err


def test_the_report_scans_the_selected_repository(repo, capsys, tmp_path, monkeypatch):
    """With --repo, the query and the scan use that repository, not the working directory."""
    repo.write('guide.md', '# Guide\n')
    repo.write('README.md', '# Project\n\n[g](guide.md)\n')
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    (elsewhere / 'guide.md').write_text('# Decoy\n\n[g](guide.md)\n', encoding='utf-8')
    monkeypatch.chdir(elsewhere)
    assert report(repo, capsys, 'guide.md')[1][1:-2] == ['README.md:3']


def test_the_report_resolves_the_query_from_the_root(repo, capsys, monkeypatch):
    """From a subdirectory, the query is still a path from the root of the repository."""
    repo.write('docs/guide.md', '# Guide\n')
    repo.write('README.md', '# Project\n\n[g](docs/guide.md)\n')
    monkeypatch.chdir(repo.root / 'docs')
    assert testee.main(['--references', 'docs/guide.md']) == 0
    assert 'README.md:3' in capsys.readouterr().out
    assert testee.main(['--references', 'guide.md']) == 2
    capsys.readouterr()


def test_symbolic_links_cannot_reach_outside_the_repository(repo, capsys):
    """A link or a query through a symbolic link that leaves the repository is rejected, and nothing is read there."""
    outside = repo.root.parent / 'outside'
    outside.mkdir()
    (outside / 'secret.md').write_bytes(b'\xff\xfe not text')
    (repo.root / 'door').symlink_to(outside, target_is_directory=True)
    (repo.root / 'alias.md').symlink_to(outside / 'secret.md')
    repo.write('README.md', '# Project\n\n[s](door/secret.md#part)\n\n[t](alias.md#part)\n')
    assert repo.check(capsys) == (1, [
        'README.md:3: link: door/secret.md#part is not a relative path in the repository',
        'README.md:5: link: alias.md#part is not a relative path in the repository'])
    for target in ('door/secret.md', 'door/secret.md#part', 'alias.md', 'door'):
        assert testee.main(['--repo', str(repo.root), '--references', target]) == 2
        assert f'{target} names no file or directory in the repository' in capsys.readouterr().err


def test_a_symbolic_link_inside_the_repository_names_its_target(repo, capsys):
    """A link through an internal symbolic link is a reference to the real file, under its real name."""
    repo.write('docs/guide.md', '# Guide\n')
    (repo.root / 'shortcut').symlink_to('docs', target_is_directory=True)
    repo.write('README.md', '# Project\n\n[g](shortcut/guide.md)\n')
    assert report(repo, capsys, 'shortcut/guide.md') == (0, [
        'References to docs/guide.md:', 'README.md:3', '', '1 reference found.'])


def test_the_report_runs_no_check_and_changes_nothing(repo, capsys):
    """A report succeeds in a repository with problems, and it leaves the files and the index as they were."""
    repo.write('guide.md', '# Guide\n')
    repo.write('README.md', '# Project\n\nBad; text with [g](guide.md) and [m](missing.md).\n')
    repo.git('add', 'guide.md')
    before = (repo.git('status', '--porcelain=v2'), repo.git('ls-files', '-s'), (repo.root / 'README.md').read_bytes())
    assert report(repo, capsys, 'guide.md')[0] == 0
    assert before == (
        repo.git('status', '--porcelain=v2'), repo.git('ls-files', '-s'), (repo.root / 'README.md').read_bytes())
    assert repo.check(capsys)[0] == 1


def test_an_unreadable_file_stops_the_report(repo, capsys):
    """A report that cannot scan every file is incomplete, so it gives status two and no result."""
    repo.write('guide.md', '# Guide\n')
    (repo.root / 'binary.md').write_bytes(b'\xff\xfe\x00')
    assert testee.main(['--repo', str(repo.root), '--references', 'guide.md']) == 2
    captured = capsys.readouterr()
    assert 'binary.md is not UTF-8 text' in captured.err
    assert captured.out == ''
