# Evidence Reuse

Read this file if you want to reuse gate results, mutation evidence or a verdict instead of a new run. Reuse is optional. A new run is always an alternative.

## Rules for every reuse

- Follow the provenance checks in the [working record template](review-record.md#additional-provenance-for-evidence-reuse).
- If a relevant input changed, or you do not know whether it changed, run the affected checks again.
- Identical tested content MAY support reuse after a change of commit metadata, but only for checks that do not depend on that metadata.

## Cosmetic follow-ups

A verified cosmetic follow-up MAY reuse the gates and the verdict. The change is cosmetic only if it cannot change any of these:

- Behavior.
- Test guarantees.
- Build or configuration output.
- Security.
- Concurrency.
- State or data flow.
- User-visible contracts.

Before you reuse results for a cosmetic follow-up:

1. Check the relevant dependencies, environment, file modes and symlink targets.
2. Run the lint, format and diff checks that the change affects.
3. Record why the results are equivalent, and record the updated fingerprints.
4. Keep the earlier dispositions.

Removal of blank lines at the end of a file is usually cosmetic. Whitespace in strings or configuration is not automatically cosmetic, and neither is a comment that a tool reads as a directive.

If you are uncertain whether a change is cosmetic, run the full gates and the review.
