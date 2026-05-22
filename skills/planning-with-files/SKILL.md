---
name: planning-with-files
description: "Use this skill to choose and maintain the right planning mode for a task. skip creates no artifact, light writes only session state.json, and full writes session state.json plus global_plan/current/plan.md when approval is required. Pair with prd when product scope or acceptance criteria need definition before execution."
---

# Planning With Files

Use the lightest planning mode that fits the task. `PRD.md` remains the product source of truth when product scope or acceptance criteria are involved; execution state belongs in `update_plan_state`.

## Planning Mode Self-Check

Before substantive work, estimate task size and risk:

| Mode | Use When | Artifacts |
|---|---|---|
| `skip` | <=2 low-risk actions, <=1 changed file, no cross-module/state/middleware/tool schema/TUI/persistence/permission risk, no need for user plan confirmation | None |
| `light` | 3-5 actions or 2-3 changed files, clear goal, low risk, no need for a readable plan.md | `.harness/sessions/<session-id>/planning/state.json` |
| `full` | >5 actions, >3 changed files, cross-module work, state/middleware/tool schema/TUI/persistence/permission/rollback risk, or user plan confirmation is needed | `state.json` and, when approval is required, `global_plan/current/plan.md` |

Rules:

- `skip` does not call `update_plan_state` and does not write JSON or Markdown artifacts.
- `light` calls `update_plan_state(update_kind="start")` before tracked actions.
- `full` calls `update_plan_state(update_kind="start", requires_approval=true)` with `plan_markdown`, then waits for user confirmation before tracked actions.
- `start`, `replan`, and `final` updates are mandatory in `light/full`.
- `progress` updates are written only at key milestones, not after every action.

## Action Definition

Count an action when a tool call advances execution state:

- File changes: `apply_patch`, `write_file`.
- Execution commands: tests, builds, formatters, generators, installs, services, migrations through `run_bash`.
- Verification actions: browser checks, screenshots, UI interaction tests.
- Read-only consultation: `consult_subagent`.

Do not count read-only investigation, ordinary assistant messages, `/plan` read-only investigation, or `update_plan_state`.

## Replan

Use `update_plan_state(update_kind="replan")` when assumptions break, repeated failures occur, requirements change, or the actual scope exceeds the initial mode.

- `requires_approval=false`: technical replan only. Write `state.json` and continue.
- `requires_approval=true`: user-visible plan changed. Write `state.json` and `global_plan/current/plan.md`, then wait for confirmation.

## Final Update

Before stopping in `light/full`, call `update_plan_state(update_kind="final")` and include:

- `result_status`
- `validation`
- `remaining_issues`

Do not create `status.md`, `final.md`, root `task_plan.md`, root `findings.md`, or root `progress.md` as planning outputs.
