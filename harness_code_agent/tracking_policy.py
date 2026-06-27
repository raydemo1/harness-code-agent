"""Shared planning mode policy for runtime profiles and skill routing."""

PLANNING_MODE_POLICY = """\
## Task Tracking
Choose the lightest tracking path that still makes the work reliable before using substantive action tools.

- Use skip for at most 2 low-risk actions affecting at most 1 file. Skip creates no tracking artifact and does not call update_plan_state.
- Use tracked for non-trivial work. Call update_plan_state(mode="tracked", update_kind="start") before tracked actions.

Tracked steps are execution todos plus acceptance state, not a formal plan or approval gate. Replan when an assumption breaks, failures repeat, requirements change, or the real scope outgrows the current todo list. Recovery replans enter PROBE mode: next_action is one low-cost read-only verification command, and edits resume only after that probe succeeds.

Update progress at meaningful milestones rather than after every action. Before stopping in tracked mode, call update_plan_state(update_kind="final") with result_status, validation, and remaining_issues.
"""

PLANNING_MODE_CATALOG_POLICY = (
    "Use the built-in Task Tracking Self-Check before substantive work: skip writes no "
    "artifact for tiny low-risk tasks, and tracked writes session state.json through "
    "update_plan_state. Formal plan.md files and approval belong to interactive planning "
    "flows, not update_plan_state. Execution state, retries, and verification belong in "
    "tracked updates, not in PRD.md."
)
