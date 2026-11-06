#! /bin/bash
# Run the test suite and enforce the coverage policy that AGENTS.md delegates to this script:
# statement and branch coverage must each reach the threshold for every measured file and for the
# project as a whole. coverage.py reports line coverage as statement coverage, so the two are one
# check here. pytest's own --cov-fail-under only guards the project total, which is why per-file
# enforcement lives here instead of in pyproject.toml.
#
# Usage: ./check-coverage.sh [pytest options...]
# Extra arguments are forwarded to pytest. Selecting a subset of tests makes the per-file gate
# meaningless, so pass options (e.g. -x, -q), not test paths.
#
# Environment:
#   COVERAGE_THRESHOLD  Minimum percentage per file and for the total, from 0 to 100 inclusive.
#                       Defaults to 80 when unset or empty; an invalid value fails before pytest runs.
set -uo pipefail

threshold="${COVERAGE_THRESHOLD:-80}"
# Reject an unusable threshold before running the suite: NaN compares false against every
# percentage, and an out-of-range value makes the per-file comparisons pass or fail vacuously.
python3 - "$threshold" <<'PY' || exit 1
"""Exit with a message unless the threshold is a number in [0, 100]."""
import sys

raw_threshold = sys.argv[1]
try:
    threshold = float(raw_threshold)
except ValueError:
    threshold = float('nan')
# The chained comparison is false for NaN and both infinities, so it also enforces finiteness.
if not 0 <= threshold <= 100:
    sys.exit(f"check-coverage: COVERAGE_THRESHOLD must be a number from 0 to 100, got {raw_threshold!r}")
PY

report="$(mktemp -t coverage-XXXXXXXX.json)"
trap 'rm -f "$report"' EXIT

pytest --cov-report="json:$report" "$@"
pytest_status=$?
if [ ! -s "$report" ]; then
    echo "check-coverage: pytest produced no coverage report (exit ${pytest_status})" >&2
    exit 1
fi

python3 - "$report" "$threshold" <<'PY'
"""Report every measured file whose statement or branch coverage is below the threshold."""
import json
import sys

report_path, raw_threshold = sys.argv[1], sys.argv[2]
threshold = float(raw_threshold)
with open(report_path, encoding='utf-8') as handle:
    report = json.load(handle)


def percentages(summary):
    """Return (statement %, branch %) for a coverage summary, with None where nothing is measured."""
    statements = summary['num_statements']
    branches = summary['num_branches']
    statement_percent = None if statements == 0 else 100.0 * summary['covered_lines'] / statements
    branch_percent = None if branches == 0 else 100.0 * summary['covered_branches'] / branches
    return statement_percent, branch_percent


failures = []
for path, data in sorted(report['files'].items()):
    statement_percent, branch_percent = percentages(data['summary'])
    if statement_percent is None:  # An empty module such as __init__.py measures nothing.
        continue
    if statement_percent < threshold or (branch_percent is not None and branch_percent < threshold):
        failures.append((path, statement_percent, branch_percent))

total_statement, total_branch = percentages(report['totals'])
if total_statement is not None and total_statement < threshold:
    failures.append(('TOTAL', total_statement, total_branch))
elif total_branch is not None and total_branch < threshold:
    failures.append(('TOTAL', total_statement, total_branch))

if failures:
    print(f"\nCoverage below {threshold:g}% (statement/branch):", file=sys.stderr)
    for path, statement_percent, branch_percent in failures:
        branch_text = 'n/a' if branch_percent is None else f'{branch_percent:.1f}%'
        print(f"  {path}: {statement_percent:.1f}% / {branch_text}", file=sys.stderr)
    sys.exit(1)

total_branch_text = 'n/a' if total_branch is None else f'{total_branch:.1f}%'
print(f"\nCoverage gate passed: every file and the total reach {threshold:g}% "
      f"(total {total_statement:.1f}% statement / {total_branch_text} branch).")
PY
coverage_status=$?

if [ "$pytest_status" -ne 0 ]; then
    exit "$pytest_status"
fi
exit "$coverage_status"
