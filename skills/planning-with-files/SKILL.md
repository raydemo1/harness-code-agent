---
name: planning-with-files
description: "Use this skill to choose and maintain the right planning mode for a task. It provides the agent's planning/progress workflow: skip for <3 estimated tool calls, light for 3-5 using only progress.md, and full for >5 using task_plan.md, findings.md, and progress.md. Pair with prd when product scope or acceptance criteria need definition before execution."
---

# Planning With Files

Use persistent markdown files as the agent's working memory on disk.

This skill pairs with `prd`:
- `PRD.md` says what should be built and how success will be judged.
- `planning-with-files` says how execution is proceeding, using the lightest mode that fits the task.

Do not put execution logs into `PRD.md`. Do not put product scope changes only into planning files.

## Planning Mode Self-Check

Before substantive work, estimate the task size and choose a mode:

| Mode | Estimate | Files | User-visible? |
|---|---|---|---|
| `skip` | Fewer than 3 tool calls | No required files | No |
| `light` | 3-5 tool calls | `progress.md` only | Yes, one short sentence |
| `full` | More than 5 tool calls | `task_plan.md`, `findings.md`, `progress.md` | Yes, one short sentence |

Call `update_planning_files` with the selected mode before action tools.

Use `skip` for simple questions, tiny edits, and single-command checks. Use `light` for small but real execution that needs a durable status. Use `full` for multi-step implementation, research-heavy work, broad refactors, high-risk work, or anything likely to exceed five tool calls.

If product scope, MVP, non-goals, workflows, or acceptance criteria are fuzzy, use `prd` first. After `PRD.md` is written or updated, choose `skip` / `light` / `full` for the execution slice.

## Collaboration Contract With PRD

When `PRD.md` exists, read it before making or updating the execution plan. Treat it as the product source of truth.

When scope, requirements, acceptance criteria, risk boundaries, or the first slice changes, update `PRD.md` with the `prd` skill. Then update the planning files to reflect the new execution path.

When only implementation status, research findings, errors, retries, test results, or next steps change, update the planning files only.

Use this handoff shape when moving from PRD to execution:

```md
PRD goal:
Current slice:
Acceptance criteria:
Execution phases:
Verification:
Known risks:
```

## File Responsibilities

| File | Mode | Purpose |
|---|---|---|
| `PRD.md` | PRD pass | Product scope, non-goals, acceptance criteria, user workflows, constraints, first implementation slice. |
| `progress.md` | light/full | The single progress file: goal, steps, current step, blockers, next action, and update count. |
| `task_plan.md` | full | Phase plan, current phase, decisions, errors, status. |
| `findings.md` | full | Research notes, codebase discoveries, technical findings, relevant references. |

There is only one progress file: `progress.md`. Do not create a second progress diary. In full mode, `task_plan.md` and `findings.md` support execution context but do not replace `progress.md`.

## Startup Procedure

Before complex work:

1. Check for a previous session with `scripts/session-catchup.py` when practical.
2. Read `PRD.md` if it exists.
3. Choose planning mode with the self-check.
4. Call `update_planning_files` before edits or commands that perform work.

Windows example, using an explicit Python interpreter:

```powershell
& "D:\miniconda\miniconda\python.exe" "skills\planning-with-files\scripts\session-catchup.py" (Get-Location)
```

If the catchup report shows unsynced context:
1. Run `git diff --stat`.
2. Read current planning files.
3. Update planning files based on catchup plus the actual diff.
4. Continue from the current phase.

## Working Loop

Use this loop during execution:

1. Re-read `task_plan.md` before major decisions.
2. In full mode, save important discoveries to `findings.md` when they change the implementation path.
3. In full mode, update `task_plan.md` after each phase or strategy change.
4. In light/full mode, update `progress.md` after errors, strategy changes, verification, and before final response.
5. Do not update files on every minor read; use event-triggered updates so planning stays useful without slowing small work.

## Error Protocol

Never repeat the same failing action unchanged.

Use three attempts:
- Attempt 1: diagnose the concrete error and patch the likely cause.
- Attempt 2: try a different tool, entry point, or narrower reproduction.
- Attempt 3: re-check assumptions, inspect docs or surrounding code, and update the plan.

After three failed attempts, summarize what failed, what changed between attempts, and what input is needed from the user.

## Templates And Scripts

Use these bundled resources only when useful:
- `templates/task_plan.md`
- `templates/findings.md`
- `templates/progress.md`
- `scripts/init-session.ps1` / `scripts/init-session.sh`
- `scripts/check-complete.ps1` / `scripts/check-complete.sh`
- `scripts/session-catchup.py`

Planning files go in the project root, not inside the skill directory.

## Anti-Patterns

Avoid:
- Treating `PRD.md` as a diary.
- Letting planning files become the only place where scope changed.
- Creating planning files and then never updating them.
- Hiding failed attempts from the plan.
- Stopping with pending phases unless the user explicitly pauses or blocks the work.
