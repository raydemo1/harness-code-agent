"""Shared task tracking policy for runtime profiles and skill routing."""

TASK_TRACKING_POLICY = """\
## Task Tracking
Choose the lightest coordination mode that keeps the work reliable. Judge the nature of the task rather than counting files, tools, commands, or tests.

- Use skip when the task can be completed directly and progress tracking would add no useful information. Skip creates no tracking artifact and does not call update_plan_state.
- Use todo for clear work with several natural execution steps where lightweight progress visibility helps. Call update_plan_state(mode="todo", update_kind="start") with a short execution checklist. Todo mode is display only: do not add acceptance_checks, do not run a plan audit, and do not require replans or a final state update.
- Use tracked only when the work has meaningful uncertainty, risk, dependent phases, evolving acceptance criteria, or the user explicitly requests a formal plan. Tracked start includes concrete acceptance_checks grounded in the task text; each check needs a verification_command that can fail.

Running tests, editing several related files, or using several tools does not by itself make a task complex. Escalate from todo to tracked only when execution reveals materially greater uncertainty, risk, or coordination needs.

Tracked steps are execution todos plus acceptance state, not a formal plan or approval gate. In tracked mode, replan when an assumption breaks, failures repeat, requirements change, or the verification strategy materially changes. Recovery replans enter PROBE mode: next_action is one low-cost read-only verification command, and edits resume only after that probe succeeds.

Update progress at meaningful milestones rather than after every action. Before stopping in tracked mode, call update_plan_state(update_kind="final") with result_status, validation, remaining_issues, and one check_result for every active acceptance check.
"""

TASK_TRACKING_CATALOG_POLICY = (
    "Use the built-in Task Tracking Self-Check before substantive work: skip writes no "
    "artifact for direct work, todo provides lightweight progress visibility, and tracked "
    "adds acceptance state only when uncertainty or risk justifies it. Both todo and tracked "
    "write session state.json through update_plan_state. Formal plan.md files and approval belong to interactive planning "
    "flows, not update_plan_state. Execution state, retries, and verification belong in "
    "tracked updates, not in product specs or ticket documents."
)
