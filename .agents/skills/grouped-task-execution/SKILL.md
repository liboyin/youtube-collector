---
name: grouped-task-execution
description: Select and implement a small group of related open tasks and review the integrated group. Use for grouped backlog execution in this repository.
---

# Grouped Task Execution

Use this workflow for a group of related backlog tasks.

The main agent implements the group itself, without implementation subagents. The main agent also owns the integration, the final validation and the commit boundaries.

## 1. Select and bound

1. Choose a small, high-value group of tasks that share a behavior, an invariant or a dependency.
2. Revalidate each task against the current code.
3. Bound each task under the [task rules](../../../tasks/README.md#bound-a-task). Keep one authoritative boundary for each task in `tasks/open/<ID>.md`.
4. Record the intended commit boundaries and their order before implementation.

Grouping does not expand authorization, and it does not settle an unresolved material decision.

## 2. Implement in dependency order

- Implement foundations before the behavior that depends on them.
- Run focused checks between stages.

## 3. Integrate and freeze

1. Resolve the integration issues.
2. Compare the work with the plan from step 1, and record each deviation.
3. Confirm the commit boundaries. [Review Scope](../adversarial-review/SKILL.md#review-scope) says when tasks may share one review.
4. Freeze the candidate and run the final gates. Isolated evidence work MAY run at the same time.

These rules also apply:

- Freeze, validate and review each independent candidate in the order from step 1.
- An intermediate state needs full gates and its own review only if it is committed independently.
- Independent candidates MAY be reviewed in parallel, as [parallel candidates](../adversarial-review/references/parallel-candidates.md) describes.

## 4. Review and resolve

- Apply the [review procedure](../adversarial-review/SKILL.md#procedure) to the integrated group.
- [Review Scope](../adversarial-review/SKILL.md#review-scope) says what the review covers, and when tasks that were already reviewed need a further review.

## 5. Close

1. Verify the done criteria of each task.
2. Close the tasks under the [task rules](../../../tasks/README.md#close-a-task) before the commit.

## 6. Gap review

After the group's commits, the main agent reviews the committed range itself. This step does not use the adversarial review skill.

1. Check the range against the accepted boundaries, contracts, decisions, completion records, documentation and conventions.
2. Look especially for risks that only the combined changes create.
3. Report each gap with its location, evidence and impact. Separate defects from hypotheses and from nits.
4. Report the gaps before you fix them, unless the user asked for fixes. A gap does not expand authorization.
5. Fix each gap under the normal rules for boundaries, gates, review and completion records.
6. Repeat until no gap remains, or until each gap has a recorded disposition.

## 7. Report and reflect

- Report the changes, checks, verdicts, gap-review outcome, deferrals and limitations. Link the completion records.
- End the handoff with the governance friction that you observed and concrete improvements. Separate friction from your own execution mistakes.
- If no friction arose, say so briefly.
- Treat this feedback as a proposal. [Governance Files](../../../AGENTS.md#governance-files) says where a lesson goes and who approves a governance edit.
