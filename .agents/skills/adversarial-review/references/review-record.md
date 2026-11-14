# Review Record

Use this template for the working record of a [review](../SKILL.md#procedure). [Evidence Retention](../SKILL.md#evidence-retention) says where the record lives and what it keeps.

- Update the record for each follow-up candidate.
- Record decisions and results, not a transcript.
- Omit a section that does not apply.

## Fields

- **Boundary:** a link to the accepted boundary in `tasks/open/<ID>.md`. Distinguish the existing contracts of callers from new requirements for runtime validation.
- **Target:** the starting revision and the candidate revision or range, the in-scope paths and the uncommitted inputs. For a follow-up, add the earlier target.
- **Reviewer:** the session ID, the model and the effort. If the reviewer was replaced, add the reason and the transferred findings.
- **Checks:** the commands, the results, a summary of the relevant environment without secrets, and the reason for the choice of baseline.
- **Coverage decision:** for a group, whether a further group review is needed, and why.
- **Findings:** the reviewer's report and verdict, the main agent's validation and dispositions, and the user's decisions. For each deferral, add the reason and the revisit trigger.
- **Integrity and cleanup:** the result of the snapshot comparison, the cleanup performed, and the location and lifetime of each retained artifact.

## Mutation evidence

Fill this section if mutation evidence was required or performed.

- Name the primary producer, and list what the reviewer added.
- Link the mutants file and the evidence file that `mutate.py` wrote. The evidence file identifies the candidate, each mutant, its tests, the provenance and the failure.
- For removed or weakened coverage, name the retained test that still rejects the broken behavior.
- If the candidate, a test or a probe changed, state which evidence the change invalidates and why the other cases still apply.
- The reviewer adds the reason for each rerun or added case.

## Evidence reuse

Fill this section only if results were reused. [Evidence reuse](evidence-reuse.md#what-to-record) lists what to record.
