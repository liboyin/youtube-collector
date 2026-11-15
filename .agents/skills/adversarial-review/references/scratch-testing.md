# Scratch Testing Recipe

Read this recipe before the first scratch run of a review.

[mutate.py](mutate.py) does the mechanical work. It makes an isolated scratch copy, runs each selection in a fresh process, checks where modules came from, and writes the evidence. Its `--help` text describes the modes and the mutants file. This recipe covers the choices that the tool cannot make.

## Choose the tests

- Select the narrowest tests that observe the protected invariant, for each mutant.
- If you probe premature acknowledgement or publication, make the mutant perform that side effect. A mutant that only prepares the operation proves nothing, unless the preparation is the protected invariant.
- If you show that a new test fails on the code before the change, the test MUST NOT use names that the change introduces. Give the test its own copies of those values.

## Contain the side effects

A mutant can reach code that the passing test never ran. For example, a mutant that bypasses validation can reach the creation of a resource.

- Before the run, inspect the failure paths and routing paths that the mutant changes.
- The selected tests must own every side effect that the mutant can reach.
- If a path is not owned, supply a service double or a guard. Add it to the scratch copy with `--add`.
- When adding a probe or guard, preserve the existing test bootstrap, fixtures and hooks. This includes each `conftest.py` file.
- If the probe intentionally changes a safeguard, record that change and contain its side effects separately.
- An invalid hostname, synthetic credentials or temporary settings do not prevent a network attempt.
- If you add a guard, preserve the import lifecycle under test.
- A mutant that weakens a guard removes that guard's protection. Its probes MUST target destinations that reach nothing, as the [network conventions](../../../../tests/README.md#network) describe.

## Limits of the provenance check

The tool checks module locations in the pytest process, at the end of the session.

- The check does not cover a child process that a test starts. Add a location check inside each child with `--add`.
- If a test unloads or replaces a module, check the location immediately after the import that the test controls.

## Mutants that break a stop signal

A mutant that breaks a stop signal or a shutdown signal can leave threads alive. The evidence lists them.

- Record the leaked threads as the mutant's behavioral effect.
- Give that mutant only the test that observes the invariant, so that the leaked work cannot reach another test.
- After the run, verify that the process exited and that no owned process remains.
- A run in which leaked work could reach another test is invalid evidence. Repeat it with the narrower selection.

## If a run is not evidence

The tool marks a run INVALID, or exits with status 2, and writes an `advice:` line.

- Follow the advice. Do not reclassify the run by hand.

If a run escaped the tests' ownership:

1. Stop the probe and wait until the owned work is quiet.
2. Correct the containment or the test selection before you retry.
3. Record briefly what escaped, what it accessed and what you cleaned up. Do not record secrets.
4. Rerun only the affected cases, as [Evidence Retention](../SKILL.md#evidence-retention) describes.
