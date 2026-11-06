"""Exercise the coverage gate script with an owned pytest stub and synthetic coverage reports."""

import json
import shlex
import shutil
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'check-coverage.sh'


def summary(statements, covered_lines, branches, covered_branches):
    """Build a coverage.py JSON summary with the keys the gate reads.

    Args:
        statements: Number of measured statements.
        covered_lines: Number of executed statements.
        branches: Number of measured branches.
        covered_branches: Number of taken branches.

    Returns:
        A summary mapping in coverage.py's JSON report shape.
    """
    return {
        'num_statements': statements,
        'covered_lines': covered_lines,
        'num_branches': branches,
        'covered_branches': covered_branches,
    }


def report(files):
    """Build a coverage.py JSON report whose totals sum the given per-file summaries.

    Args:
        files: Mapping of measured path to its summary.

    Returns:
        A report mapping with ``files`` and ``totals``.
    """
    keys = ('num_statements', 'covered_lines', 'num_branches', 'covered_branches')
    totals = {key: sum(data[key] for data in files.values()) for key in keys}
    return {'files': {path: {'summary': data} for path, data in files.items()}, 'totals': totals}


def run_gate(runner, *, coverage_report=None, pytest_status=0, threshold=None, args=()):
    """Run the gate through the shared owned-subprocess runner with a ``pytest`` stub first on ``PATH``.

    The stub never runs tests: it records its arguments, copies the synthetic report (if any) to
    the ``--cov-report=json:<path>`` destination and exits with ``pytest_status``.

    Args:
        runner: The shared shell-script runner; its working directory holds the stub and its records.
        coverage_report: Report mapping for the stub to write, or None to write no report.
        pytest_status: Exit status of the stub.
        threshold: ``COVERAGE_THRESHOLD`` value, or None to leave it unset.
        args: Arguments forwarded to the gate.

    Returns:
        The completed process and the stub's recorded arguments, or None if the stub never ran.
    """
    python3 = shutil.which('python3')
    assert python3 is not None, 'python3 is required by the gate script'
    stub_dir = runner.cwd / 'bin'
    stub_dir.mkdir()
    invocations = runner.cwd / 'pytest-invocation'
    source = runner.cwd / 'report-source.json'
    if coverage_report is not None:
        source.write_text(json.dumps(coverage_report), encoding='utf-8')
    stub = stub_dir / 'pytest'
    stub.write_text(
        '#!/bin/bash\n'
        f'printf "%s\\n" "$@" > {shlex.quote(str(invocations))}\n'
        'for arg in "$@"; do\n'
        '    case "$arg" in\n'
        '        --cov-report=json:*)\n'
        f'            if [ -e {shlex.quote(str(source))} ]; then\n'
        f'                cp {shlex.quote(str(source))} "${{arg#--cov-report=json:}}"\n'
        '            fi ;;\n'
        '    esac\n'
        'done\n'
        f'exit {pytest_status}\n',
        encoding='utf-8',
    )
    stub.chmod(0o755)
    env = {} if threshold is None else {'COVERAGE_THRESHOLD': threshold}
    result = runner(SCRIPT, *args, path_dirs=(stub_dir, Path(python3).parent), env=env)
    # The gate must remove its mktemp report from the owned TMPDIR on every path.
    assert list(runner.temp.iterdir()) == []
    recorded = invocations.read_text(encoding='utf-8').splitlines() if invocations.exists() else None
    return result, recorded


PASSING_REPORT = report({'pkg/a.py': summary(100, 90, 20, 18), 'pkg/b.py': summary(50, 45, 10, 9)})
DEFAULT_THRESHOLDS = pytest.mark.parametrize('threshold', [None, '', '80'], ids=['unset', 'empty', 'explicit'])


@pytest.mark.parametrize('threshold', ['nan', 'NaN', 'inf', '-inf', 'infinity', '-1', '-0.5', '100.1', '1e3', 'eighty', '80%'])
def test_invalid_threshold_fails_clearly_without_running_pytest(run_shell_script, threshold):
    """Non-numeric, non-finite and out-of-range thresholds exit non-zero with a message before pytest runs."""
    result, recorded = run_gate(run_shell_script, coverage_report=PASSING_REPORT, threshold=threshold)

    assert recorded is None
    assert result.returncode != 0
    assert result.stderr.startswith('check-coverage: COVERAGE_THRESHOLD')
    assert repr(threshold) in result.stderr
    assert result.stdout == ''


@pytest.mark.parametrize(
    ('threshold', 'coverage_report'),
    [
        ('0', report({'pkg/a.py': summary(10, 0, 4, 0)})),
        ('100', report({'pkg/a.py': summary(10, 10, 4, 4)})),
    ],
    ids=['zero-with-no-coverage', 'hundred-with-full-coverage'],
)
def test_boundary_thresholds_are_accepted_and_applied(run_shell_script, threshold, coverage_report):
    """Thresholds of 0 and 100 are valid and gate a report at exactly that coverage."""
    result, recorded = run_gate(run_shell_script, coverage_report=coverage_report, threshold=threshold)

    assert recorded is not None
    assert result.returncode == 0, result.stderr
    assert f'every file and the total reach {threshold}%' in result.stdout


def test_hundred_threshold_still_rejects_incomplete_coverage(run_shell_script):
    """The inclusive upper bound keeps comparing files rather than accepting everything."""
    result, _ = run_gate(run_shell_script, coverage_report=report({'pkg/a.py': summary(10, 9, 4, 4)}), threshold='100')

    assert result.returncode == 1
    assert 'pkg/a.py: 90.0% / 100.0%' in result.stderr


def test_pytest_receives_owned_report_path_then_forwarded_arguments(run_shell_script):
    """The gate passes a JSON report path inside TMPDIR followed by the caller's options unchanged."""
    _, recorded = run_gate(run_shell_script, coverage_report=PASSING_REPORT, args=('-q', '-p', 'no:cacheprovider'))

    assert recorded is not None
    report_option, *forwarded = recorded
    assert report_option.startswith(f'--cov-report=json:{run_shell_script.temp}/coverage-')
    assert forwarded == ['-q', '-p', 'no:cacheprovider']


def test_missing_report_fails_with_pytest_status(run_shell_script):
    """A pytest run that writes no report fails the gate and names pytest's exit status."""
    result, recorded = run_gate(run_shell_script, pytest_status=4)

    assert recorded is not None
    assert result.returncode == 1
    assert 'check-coverage: pytest produced no coverage report (exit 4)' in result.stderr


@DEFAULT_THRESHOLDS
def test_reports_at_or_above_default_threshold_pass(run_shell_script, threshold):
    """With the default 80, files above it and a file exactly at it pass the gate."""
    at_threshold = report({'pkg/a.py': summary(100, 90, 20, 18), 'pkg/b.py': summary(100, 80, 50, 40)})

    result, _ = run_gate(run_shell_script, coverage_report=at_threshold, threshold=threshold)

    assert result.returncode == 0, result.stderr
    assert 'Coverage gate passed: every file and the total reach 80%' in result.stdout
    assert result.stderr == ''


@DEFAULT_THRESHOLDS
@pytest.mark.parametrize(
    ('low', 'expected'),
    [(summary(100, 79, 10, 10), 'pkg/b.py: 79.0% / 100.0%'), (summary(100, 100, 10, 7), 'pkg/b.py: 100.0% / 70.0%')],
    ids=['statements', 'branches'],
)
def test_single_file_below_default_threshold_fails_despite_passing_total(run_shell_script, threshold, low, expected):
    """A file below 80% statement or branch coverage fails the gate and is named while the total passes."""
    coverage_report = report({'pkg/a.py': summary(400, 400, 100, 100), 'pkg/b.py': low})

    result, _ = run_gate(run_shell_script, coverage_report=coverage_report, threshold=threshold)

    assert result.returncode == 1
    assert 'Coverage below 80% (statement/branch):' in result.stderr
    assert expected in result.stderr
    assert 'pkg/a.py' not in result.stderr
    assert 'TOTAL' not in result.stderr


def test_pytest_failure_status_takes_precedence_over_passing_coverage(run_shell_script):
    """A failing pytest exit status is returned even when the coverage comparison passes."""
    result, _ = run_gate(run_shell_script, coverage_report=PASSING_REPORT, pytest_status=3)

    assert result.returncode == 3
    assert 'Coverage gate passed' in result.stdout
