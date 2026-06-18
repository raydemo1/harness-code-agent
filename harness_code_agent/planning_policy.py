"""Shared planning mode policy for runtime profiles and skill routing."""

PLANNING_MODE_POLICY = """\
Planning Mode Self-Check:
- Run this self-check before substantive action tools.
- skip: use for <=2 low-risk actions and <=1 changed file. It writes no artifact and does not call update_plan_state.
- light: use for 3-5 actions or 2-3 changed files. Call update_plan_state(update_kind="start") before tracked actions.
- full: use for >5 actions, >3 changed files, cross-module work, state/middleware/tool schema/TUI/persistence/permission/rollback risk, or plans needing user confirmation. Call update_plan_state(update_kind="start", requires_approval=true) with plan_markdown, tell the user the plan was written to global_plan/current/plan.md, and wait for confirmation before tracked actions.
- In light/full, call update_plan_state(update_kind="replan") when assumptions break, repeated failures occur, requirements change, or the actual scope exceeds the initial mode.
- Recovery replans enter PROBE mode: make next_action one low-cost read-only verification command. Edits resume only after that probe passes.
- In light/full, write consolidated progress updates only at key milestones, not after every action.
- Before stopping in light/full, call update_plan_state(update_kind="final") with result_status, validation, and remaining_issues.
"""

PLANNING_MODE_CATALOG_POLICY = (
    "Use the built-in Planning Mode Self-Check before substantive work: skip writes no "
    "artifact for tiny low-risk tasks, light writes session state.json through "
    "update_plan_state, and full also writes global_plan/current/plan.md when user "
    "approval is required. Execution state, retries, and verification belong in "
    "update_plan_state, not in PRD.md."
)
