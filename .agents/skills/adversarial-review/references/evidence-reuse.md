# Evidence Reuse

Read this file if you want to reuse gate results, mutation evidence or a verdict instead of a new run. Reuse is optional.

## Rules for every reuse

- Record the provenance that [What to record](#what-to-record) lists.
- If a relevant input changed, or you do not know whether it changed, run the affected checks again.
- Identical tested content MAY support reuse after a change of commit metadata, but only for checks that do not depend on that metadata.

## Cosmetic follow-ups

A verified cosmetic follow-up MAY reuse the gates and the verdict. A change is cosmetic only if it cannot affect any item in the [definition of a non-trivial change](../../../../AGENTS.md#validation-and-review).

Before you reuse results for a cosmetic follow-up:

1. Check the relevant dependencies, environment, file modes and symlink targets.
2. Run the lint, format and diff checks that the change affects.
3. Record why the results are equivalent, and record the updated fingerprints.
4. Keep the earlier dispositions.

Removal of blank lines at the end of a file is usually cosmetic. Whitespace in strings or configuration is not automatically cosmetic, and neither is a comment that a tool reads as a directive.

If you are uncertain whether a change is cosmetic, run the full gates and the review.

## What to record

Record these items in the working record:

- The revision that was tested originally, the commands, the results and the location of the evidence.
- A comparison of the relevant inputs: implementation, tests, dependencies on the call path, configuration, file modes, symlink targets and environment. Include the in-scope untracked inputs.
- The fingerprints of the tested inputs.
- The environment evidence that shows equivalence, without secrets. Include tool versions and dependency versions where they are relevant.

Never read ignored content or secrets to build this provenance.
