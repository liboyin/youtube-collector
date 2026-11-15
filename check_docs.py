#!/usr/bin/env python3
"""Check the Markdown documentation: links, task files and the style rules that a tool can check.

Run it from the repository before every commit that touches a Markdown file. It prints one line for
each problem, as ``path:line: rule: message``.

Links: each relative link resolves inside the repository, and each anchor exists in its target file.

Tasks: each file in ``tasks/open/`` has one line in the index of ``tasks/TODO.md``, no ID is both open
and closed, and the title of a task file starts with the ID in its file name.

Style: the rules of STYLE.md that need no judgment. A list item is checked as an instruction (28 words
for each sentence) and other prose as a description (35 words). Code, tables, headings, block quotes and
YAML front matter are exempt. In the files that are not rewritten as a whole, only changed lines are
checked: lines that ``git diff HEAD`` adds, and every line of an untracked file.

With ``--references``, it runs no check. It lists each line that links to a file, a directory or a
heading, for example ``--references AGENTS.md`` or ``--references 'AGENTS.md#governance-files'``. The
path is relative to the root of the repository. A file query includes the links to its headings.
"""

import argparse
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

# STYLE.md (Scope) names the files that are not rewritten as a whole. Open task files are rewritten
# only when a task is bounded, so they are treated the same way.
CHANGED_LINES_ONLY = ('DESIGN.md', 'project-review-2026-11-10.md', 'tasks/open/', 'tasks/closed/')
ITEM_WORDS, PROSE_WORDS, PARAGRAPH_SENTENCES = 28, 35, 7

_ITEM = re.compile(r'\s*(?:[-*+]|\d+\.)\s+')
_FENCE = re.compile(r'\s*(`{3,}|~{3,})(.*)')
_CODE = re.compile(r'`[^`]*`')
# A destination is in angle brackets, or it has no spaces and at most one level of balanced parentheses.
_DESTINATION = r'(<[^<>]*>|(?:[^()\s]|\([^()\s]*\))*)'
_TITLE = r"""(?:\s+(?:"[^"]*"|'[^']*'|\([^()]*\)))?"""
_LINK = re.compile(r'\[([^\]]*)\]\(\s*' + _DESTINATION + _TITLE + r'\s*\)')
_REFERENCE = re.compile(r'\[([^\]]+)\]\[([^\]]*)\]')
_DEFINITION = re.compile(r'\s{0,3}\[([^\]]+)\]:\s*' + _DESTINATION + _TITLE + r'\s*$')
_SCHEME = re.compile(r'[A-Za-z][A-Za-z0-9+.-]*:')
# A sentence ends at a full stop, a question mark or an exclamation mark, also inside closing markup.
_SENTENCE_END = re.compile(r'[.!?]["\')*_\]]*\s+')
_SLASH = re.compile(r'(?<![\w/.:])[A-Za-z][\w-]*/[A-Za-z][\w-]*(?![\w/.])')
_HEADING = re.compile(r'#+\s+(.*)')
_HUNK = re.compile(r'@@ -\S+ \+(\d+)(?:,(\d+))? @@')
_INDEX = re.compile(r'- \[(T\d+)\]\(open/(T\d+)\.md\)')


@dataclass(frozen=True, order=True)
class Problem:
    """One violation, located by file and line."""

    path: str
    line: int
    rule: str
    message: str

    def __str__(self) -> str:
        """Return the problem as ``path:line: rule: message``."""
        return f'{self.path}:{self.line}: {self.rule}: {self.message}'


def git(repo: Path, *arguments: str) -> str:
    """Return the output of a git command run in ``repo``, with paths printed unquoted."""
    command = ['git', '-c', 'core.quotePath=false', '-C', str(repo), *arguments]
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout


def read(repo: Path, name: str) -> str:
    """Return the text of a file after resolving its path inside the repository.

    Raises:
        ValueError: If the resolved path leaves the repository or the file is not UTF-8 text.
    """
    target = (repo / name).resolve()
    if not target.is_relative_to(repo.resolve()):
        raise ValueError(f'{name} resolves outside the repository')
    try:
        return target.read_text(encoding='utf-8')
    except UnicodeDecodeError as error:
        raise ValueError(f'{name} is not UTF-8 text') from error


def markdown_files(repo: Path) -> list[str]:
    """Return the tracked and the untracked, non-ignored Markdown files that exist as regular files."""
    listed = git(repo, 'ls-files', '-z', '--cached', '--others', '--exclude-standard', '--', '*.md')
    names = sorted({name for name in listed.split('\0') if name})
    return [name for name in names if (repo / name).is_file() and not (repo / name).is_symlink()]


def changed_lines(repo: Path) -> tuple[dict[str, set[int]], set[str]]:
    """Return the lines that the working tree adds to each file since ``HEAD``, and the untracked files.

    A file that ``git mv`` moved keeps its unchanged lines out of the result, because the diff detects
    renames.
    """
    diff = git(repo, 'diff', 'HEAD', '--find-renames=20%', '--unified=0', '--no-color', '--no-ext-diff', '--', '*.md')
    added: dict[str, set[int]] = {}
    current: set[int] = set()
    for line in diff.splitlines():
        if line.startswith('+++ b/'):
            current = added.setdefault(line[len('+++ b/'):], set())
        elif match := _HUNK.match(line):
            start, count = int(match[1]), int(match[2] or '1')
            current.update(range(start, start + count))
    untracked = git(repo, 'ls-files', '-z', '--others', '--exclude-standard', '--', '*.md')
    return added, {name for name in untracked.split('\0') if name}


def classify(text: str) -> list[tuple[int, str, str]]:
    """Return each line as its number, its kind and its text.

    The kinds are ``item`` for a list item, ``prose`` for other running text, ``quiet`` for a line with
    links but no style rules (a table row, a heading, a block quote or a link definition) and ``skip``
    for code, YAML front matter and blank lines. A code block ends only at a fence of the character that
    opened it, at least as long, with nothing after it.
    """
    lines = text.splitlines()
    result = []
    fence = ''
    front = bool(lines) and lines[0] == '---'
    for number, line in enumerate(lines, start=1):
        marker = _FENCE.match(line)
        if front:
            front = number == 1 or line != '---'
            kind = 'skip'
        elif fence:
            closes = marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip()
            fence = '' if closes else fence
            kind = 'skip'
        elif marker:
            fence = marker[1]
            kind = 'skip'
        elif not line.strip():
            kind = 'skip'
        elif line.lstrip().startswith(('|', '#', '>')) or _DEFINITION.match(line):
            kind = 'quiet'
        else:
            kind = 'item' if _ITEM.match(line) else 'prose'
        result.append((number, kind, line))
    return result


def anchors(text: str) -> set[str]:
    """Return the anchors that the headings of a Markdown text define, as GitHub derives them."""
    seen: Counter[str] = Counter()
    result = set()
    for _, kind, line in classify(text):
        match = _HEADING.match(line.lstrip()) if kind == 'quiet' else None
        if match:
            title = _LINK.sub(r'\1', match[1]).strip().lower()
            slug = re.sub(r'[^\w\- ]', '', title).replace(' ', '-')
            result.add(f'{slug}-{seen[slug]}' if seen[slug] else slug)
            seen[slug] += 1
    return result


def links(text: str) -> list[tuple[int, str, str]]:
    """Return each link of a Markdown text as its line, its kind and its value.

    The kinds are ``inline`` for an inline link, ``definition`` for a link definition, ``reference``
    for a use of a defined label and ``undefined`` for a use of a label that has no definition. The
    value is the destination, or the label for ``undefined``. A reference carries the destination of
    the first definition of its label. A link in a code block or in inline code is not a link.
    """
    lines = [(number, _CODE.sub('', line)) for number, kind, line in classify(text) if kind != 'skip']
    definitions: dict[str, str] = {}
    for _, line in lines:
        if match := _DEFINITION.match(line):
            definitions.setdefault(match[1].lower(), match[2])
    result = []
    for number, line in lines:
        if match := _DEFINITION.match(line):
            result.append((number, 'definition', match[2]))
            continue
        result += [(number, 'inline', match[2]) for match in _LINK.finditer(line)]
        for match in _REFERENCE.finditer(_LINK.sub('', line)):
            label = (match[2] or match[1]).lower()
            defined = label in definitions
            result.append((number, 'reference' if defined else 'undefined', definitions[label] if defined else label))
    return result


def locate(repo: Path, name: str, link: str) -> tuple[Path, str, bool] | None:
    """Return the target of a link destination in the file ``name``, or None if it is not a local link.

    The result is the resolved path, the fragment and whether the destination is a relative path that
    stays in the repository. Symbolic links are resolved first, so a link through one that leaves the
    repository is not inside. A destination with a URL scheme and an empty destination are not local.
    """
    link = link[1:-1] if link.startswith('<') else link
    if not link or _SCHEME.match(link):
        return None
    path, _, fragment = unquote(link).partition('#')
    target = ((repo / name).parent / path).resolve() if path else (repo / name).resolve()
    return target, fragment, not path.startswith('/') and target.is_relative_to(repo)


def link_problem(repo: Path, name: str, link: str) -> str | None:
    """Return what is wrong with one link destination in the file ``name``, or None.

    A destination with a URL scheme is not checked.
    """
    located = locate(repo, name, link)
    if located is None:
        return None
    link = link[1:-1] if link.startswith('<') else link
    target, fragment, inside = located
    if not inside:
        return f'{link} is not a relative path in the repository'
    if not target.exists():
        return f'{link} does not exist'
    if fragment and target.suffix == '.md' and target.is_file():
        if fragment not in anchors(read(repo, str(target.relative_to(repo)))):
            return f'{link} names no heading in its target'
    return None


def check_links(repo: Path, files: list[str]) -> list[Problem]:
    """Return a problem for each relative link that does not resolve inside the repository.

    Inline links, link definitions and references to a definition are checked. The destination of a
    definition is checked once, at the definition.
    """
    problems = []
    for name in files:
        for number, kind, value in links(read(repo, name)):
            if kind == 'undefined':
                problems.append(Problem(name, number, 'link', f'[{value}] has no link definition'))
            elif kind != 'reference' and (message := link_problem(repo, name, value)):
                problems.append(Problem(name, number, 'link', message))
    return problems


def references(repo: Path, files: list[str], query: str) -> tuple[str, list[tuple[str, int]]]:
    """Return the normalized query, and the file and line of each line that links to its target.

    The query is a path from the root of the repository, with an optional ``#fragment``. Without a
    fragment, links to the target and to each of its headings match. With one, only links to that
    heading match. A use of a defined label counts at the line of the use, and a definition does not
    count. A line appears once, and the result is sorted.

    Raises:
        ValueError: If the query names no file or directory in the repository, or no heading in it.
    """
    path, _, fragment = query.partition('#')
    target = (repo / path).resolve()
    if not path or path.startswith('/') or not target.is_relative_to(repo) or not target.exists():
        raise ValueError(f'{query} names no file or directory in the repository')
    name = target.relative_to(repo).as_posix()
    if fragment and not (target.suffix == '.md' and target.is_file() and fragment in anchors(read(repo, name))):
        raise ValueError(f'{query} names no heading in {name}')
    found = set()
    for source in files:
        for number, kind, value in links(read(repo, source)):
            located = locate(repo, source, value) if kind in ('inline', 'reference') else None
            if located and located[2] and located[0] == target and fragment in ('', located[1]):
                found.add((source, number))
    return (f'{name}#{fragment}' if fragment else name), sorted(found)


def check_tasks(repo: Path, files: list[str]) -> list[Problem]:
    """Return a problem for each inconsistency between the task files and the index in ``tasks/TODO.md``.

    A repository without ``tasks/TODO.md`` has no task rules to check. An index line whose file is
    missing is a broken link, which ``check_links`` reports.
    """
    index_file = 'tasks/TODO.md'
    if index_file not in files:
        return []
    problems = []
    states = {state: {Path(name).stem: name for name in files if name.startswith(f'tasks/{state}/')}
              for state in ('open', 'closed')}
    indexed: Counter[str] = Counter()
    for number, line in enumerate(read(repo, index_file).splitlines(), start=1):
        if match := _INDEX.match(line):
            indexed[match[2]] += 1
            if match[1] != match[2]:
                problems.append(Problem(index_file, number, 'tasks', f'the label {match[1]} links to {match[2]}'))
            if indexed[match[2]] == 2:
                problems.append(Problem(index_file, number, 'tasks', f'{match[2]} has more than one index line'))
    for task, name in states['open'].items():
        if not indexed[task]:
            problems.append(Problem(name, 1, 'tasks', f'{task} has no line in the index of {index_file}'))
        if task in states['closed']:
            problems.append(Problem(name, 1, 'tasks', f'{task} is both open and closed'))
    for task, name in (states['open'] | states['closed']).items():
        first = read(repo, name).split('\n', 1)[0]
        if not first.startswith(f'# {task}:'):
            problems.append(Problem(name, 1, 'tasks', f'the title does not start with "# {task}:"'))
    return problems


def line_problems(kind: str, line: str) -> list[tuple[str, str]]:
    """Return the style rule and the message for each violation in one item or prose line."""
    text = _REFERENCE.sub(r'\1', _LINK.sub(r'\1', _CODE.sub('code', line)))
    found = []
    if ';' in text:
        found.append(('semicolon', 'a semicolon chains clauses; write two sentences'))
    if match := _SLASH.search(text):
        found.append(('slash', f'"{match[0]}" joins alternatives with a slash'))
    marker = _ITEM.match(text)
    sentences = [sentence for sentence in _SENTENCE_END.split(text[marker.end():] if marker else text) if sentence]
    limit = ITEM_WORDS if kind == 'item' else PROSE_WORDS
    for sentence in sentences:
        words = len(sentence.split())
        if words > limit:
            found.append(('sentence', f'{words} words in one sentence; the limit is {limit}: "{sentence[:40]}..."'))
    if len(sentences) > PARAGRAPH_SENTENCES:
        found.append(('paragraph', f'{len(sentences)} sentences in one paragraph; the limit is {PARAGRAPH_SENTENCES}'))
    return found


def check_style(repo: Path, files: list[str], added: dict[str, set[int]], untracked: set[str]) -> list[Problem]:
    """Return a problem for each violation of a style rule in the lines that are in scope.

    Every line of a file is in scope, except in the files that ``CHANGED_LINES_ONLY`` names: there, only
    the added lines are in scope, or every line if the file is untracked.
    """
    problems = []
    for name in files:
        partial = name.startswith(CHANGED_LINES_ONLY) and name not in untracked
        scope = added.get(name, set()) if partial else None
        previous = 'skip'
        for number, kind, line in classify(read(repo, name)):
            if kind in ('item', 'prose') and (scope is None or number in scope):
                if kind == 'prose' and previous == 'prose':
                    problems.append(Problem(name, number, 'line', 'a paragraph continues on a second source line'))
                problems += [Problem(name, number, rule, message) for rule, message in line_problems(kind, line)]
            previous = kind
    return problems


def main(argv: list[str] | None = None) -> int:
    """Check the documentation of a repository and print each problem, or print a reference report.

    Args:
        argv: CLI arguments, defaulting to the process arguments.

    Returns:
        Zero when there is no problem, one when there is at least one, two when the check cannot run.
        For a reference report: zero when it is complete, also with no match, and two otherwise.
    """
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--repo', type=Path, default=Path.cwd(), help='the repository; default: the working directory')
    parser.add_argument('--references', metavar='TARGET', help='run no check; list the lines that link to this '
                        'file, directory or FILE#heading, as a path from the root of the repository')
    args = parser.parse_args(argv)
    try:
        repo = Path(git(args.repo, 'rev-parse', '--show-toplevel').strip()).resolve()
        files = markdown_files(repo)
        if args.references is not None:
            target, found = references(repo, files, args.references)
            print(f'References to {target}:')
            for name, number in found:
                print(f'{name}:{number}')
            print(f'\n{len(found)} reference{"" if len(found) == 1 else "s"} found.')
            return 0
        added, untracked = changed_lines(repo)
        problems = check_links(repo, files) + check_tasks(repo, files) + check_style(repo, files, added, untracked)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        detail = error.stderr.strip() if isinstance(error, subprocess.CalledProcessError) else error
        print(f'check_docs: cannot check the documentation: {detail}', file=sys.stderr)
        return 2
    for problem in sorted(problems):
        print(problem)
    print(f'check_docs: files checked: {len(files)}, problems: {len(problems)}')
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
