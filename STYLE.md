# Style

This file owns the rules for writing documentation and the glossary of process terms. [AGENTS.md](AGENTS.md) owns the rules for messages to the user and for changes to governance files.

The rules follow ASD-STE100 and ISO 24495-1 loosely. Full compliance with either standard is not required.

## Scope

- Apply these rules to all documentation text that you add or change, and to messages to the user.
- Code, commands, quoted output and tables of data are exempt.
- The completion records in [tasks/closed/](tasks/closed/) stay as written. Do not rewrite them to meet these rules.

## Structure

1. Each list item or sentence states one rule.
2. A paragraph has one topic and at most seven sentences.
3. State the condition first, then the instruction: "If X, do Y."
4. Use a numbered list for steps and a bullet list for other items.
5. Each rule lives in one file. Other files link to it and do not restate it.
6. A rare path goes in its own reference file. The main file names the trigger and links to that file.
7. Use one source line for each prose paragraph. Keep the line breaks that Markdown structure needs.

## Wording

1. An instruction has at most 28 words. A description has at most 35 words.
2. Use the active voice and name the actor: the main agent, the reviewer or the user.
3. Use one term for one concept. Define each process term in the [glossary](#glossary).
4. Do not join alternatives with a slash. Write "the model and the effort", not "model/effort".
5. Do not chain clauses with semicolons. Write two sentences.
6. Explain an ID at its first use in a file, for example "T50 (network guard)".
7. Use the capitalized requirement words, such as MUST and MAY, only for requirements.

## Glossary

- **Boundary:** the statement of a change's intent, dependencies, non-goals, validation and done criteria.
- **Candidate:** the frozen set of changes that the gates and the review assess.
- **Completion record:** the committed file in `tasks/closed/` that records a finished change and its evidence.
- **Disposition:** the main agent's recorded decision on a finding: fix it, defer it, or accept it without change.
- **Finding:** a defect that the reviewer reports. Its class is Blocking, Non-blocking or Nit.
- **Gate:** one of the three required checks: `./check-coverage.sh`, `mypy youtube_collector` and `ruff check .`.
- **Governance file:** a file that tells agents how to work. [AGENTS.md](AGENTS.md#governance-files) lists them.
- **Main agent:** the agent that implements the change and reports to the user.
- **Mutant:** a deliberate defect placed in a scratch copy to show that a test fails when a property breaks.
- **Mutation evidence:** the record of mutants, the tests that ran against them, and the results.
- **Reviewer:** the independent agent that assesses a candidate and does not edit the repository.
- **Scratch copy:** a disposable copy of the candidate, outside the repository, for tests and mutants.
- **Working record:** the machine-local record of evidence that the main agent keeps during a review.
