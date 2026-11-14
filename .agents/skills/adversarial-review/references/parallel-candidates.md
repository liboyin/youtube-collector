# Parallel Candidates

Read this file if you want to keep working in the repository while a candidate is under review.

1. Freeze the candidate with [worktree_gates.py](worktree_gates.py). Name a new path outside the repository and outside every scratch copy, and name the candidate's files. The tool creates a git worktree from `HEAD` plus those files and runs the gates there.
2. Use the worktree as the review target. Nothing else may change it until the verdict.
3. Before the commit, run `worktree_gates.py <worktree> --compare`. It checks that the repository's working files still match the reviewed files.
4. If a shared file holds other uncommitted work, build the committed version from `HEAD` plus the candidate's own change. Examples are `tasks/TODO.md` and DESIGN.md.
5. After the commit, run `worktree_gates.py <worktree> --remove`. It removes the worktree only if the commit holds the reviewed content.

If a reviewed file is expected to differ, name it with `--changed` in steps 3 and 5. The usual case is the task record, which gains its results after the review.

Review candidates that touch the same files one after another, not in parallel.
