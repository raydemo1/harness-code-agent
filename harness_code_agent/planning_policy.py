"""Shared planning mode policy for runtime profiles and skill routing."""

PLANNING_MODE_POLICY = """\
## Planning Mode
Choose the lightest planning mode that still makes the work reliable before using substantive action tools.

- Use skip for at most 2 low-risk actions affecting at most 1 file. Skip creates no planning artifact and does not call update_plan_state.
- Use light for 3-5 actions or 2-3 changed files. Call update_plan_state(update_kind="start") before tracked actions.
- Use full for more than 5 actions, more than 3 changed files, cross-module work, or changes involving state, middleware, tool schemas, TUI behavior, persistence, permissions, rollback risk, or a plan that needs user confirmation. Start with requires_approval=true and plan_markdown, tell the user that global_plan/current/plan.md was written, and wait for confirmation before tracked actions.

Light and full plans are working models, not promises. Replan when an assumption breaks, failures repeat, requirements change, or the real scope outgrows the selected mode. Recovery replans enter PROBE mode: next_action is one low-cost read-only verification command, and edits resume only after that probe succeeds.

Update progress at meaningful milestones rather than after every action. Before stopping in light or full mode, call update_plan_state(update_kind="final") with result_status, validation, and remaining_issues.
"""

PLANNING_MODE_CATALOG_POLICY = (
    "Use the built-in Planning Mode Self-Check before substantive work: skip writes no "
    "artifact for tiny low-risk tasks, light writes session state.json through "
    "update_plan_state, and full also writes global_plan/current/plan.md when user "
    "approval is required. Execution state, retries, and verification belong in "
    "update_plan_state, not in PRD.md."
)
