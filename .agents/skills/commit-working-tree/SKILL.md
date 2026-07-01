---
name: commit-working-tree
description: Review git working tree changes, decide whether they belong in one commit or multiple focused commits, run the required project checks, and create commit messages with short descriptions of the changes. Use when Codex is asked to commit current changes, prepare clean commits from a dirty working tree, or split unrelated edits into separate commits before committing.
---

# Commit Working Tree

Create clean, reviewable commits from the current working tree. Prefer focused commits with short, descriptive subjects over one large mixed commit.

## Workflow

1. Inspect the current tree with `git status --short`, `git diff --stat`, and targeted diffs as needed.
1. Identify whether the changes share a single purpose.
1. If the changes do not share a clear theme, split them into separate commit groups before committing.
1. Stage only the files for one logical change at a time.
1. Run the checks required by the staged paths for that commit group.
1. Review the staged diff before committing.
1. Write a short commit subject that describes the change clearly.
1. Commit non-interactively with `git commit -m "<subject>"`.
1. Repeat for remaining groups until the intended changes are committed.

## Grouping Rules

- Prefer one commit when the staged files support one user-visible change, fix, refactor, or content update.
- Prefer multiple commits when the tree mixes unrelated fixes, refactors, generated files, content updates, or experiments.
- Keep schema or content validation changes grouped with the code or content they support unless they are clearly unrelated.
- If a file contains mixed concerns and cannot be safely split with non-interactive staging, pause and explain the risk instead of making a misleading commit.

## Required Checks

Determine required checks from the paths in the commit group you are about to commit. For **this repository**, use the root `Makefile` targets below. If you are reusing this skill in another repo, read that repo’s `Makefile` or CI config and substitute the equivalent commands.

- Run **`make lint-md`** if any `*.md` file in the commit group changed.
- Run **`make lint-yaml`** if any `*.yml` or `*.yaml` file in the commit group changed.
- You may run **`make lint`** once to run both markdown and YAML checks when either kind of file changed.
- Run **`make test`** if any `*.py` file in the commit group changed (syntax check via `py_compile`).
- Run **`make check-parity`** if any of these changed: `mcp_rmm_server.py`, `rmm_tools.py`, `rmm_cli.py`, `server_rmm.py`, `web/app.js`, or `scripts/check_operator_parity.py`.
- You may run **`make check`** once to run test, check-parity, and lint together.

Install lint tools once with **`make install-lint`** if `pymarkdown` or `yamllint` is missing.

Run the union of all checks that apply to the staged paths. Do not commit if a required check fails unless the user explicitly asks to commit despite failures.

## Commit Message Rules

- Use a short subject line.
- Describe the change, not the process.
- Prefer specific verbs such as `fix`, `add`, `update`, `refactor`, or `remove` when they fit naturally.
- Avoid vague subjects such as `misc updates` or `changes`.
- If creating multiple commits, tailor each subject to that commit's theme instead of reusing one generic message.

## Safety Rules

- Never include unrelated tracked changes in the same commit just to keep the tree moving.
- Never discard or revert user changes unless explicitly asked.
- If the tree contains changes you do not understand, inspect them before staging.
- If checks are expensive, still run the required ones before committing.
- Report what was committed and which checks were run.
