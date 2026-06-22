import os
import shutil
import unittest
from unittest.mock import Mock
from unittest.mock import patch

from harness_code_agent import config
from harness_code_agent.agent.runtime_state import AgentRuntimeState
from harness_code_agent.runtime.middleware.acceptance_review import (
    AcceptanceReviewMiddleware,
    ReviewOutcome,
)
from harness_code_agent.runtime.tools import execute_tool_result
from harness_code_agent.sessions.events import EventBus


class AcceptancePlanningTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = os.path.join(os.getcwd(), "workspace", "test-acceptance-planning")
        self.old_workspace = config.WORKSPACE
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        os.makedirs(self.temp_dir, exist_ok=True)
        config.WORKSPACE = self.temp_dir

    def tearDown(self):
        config.WORKSPACE = self.old_workspace
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _args(self, **overrides):
        args = {
            "mode": "light",
            "update_kind": "start",
            "goal": "fix task",
            "steps": ["inspect", "edit", "verify"],
            "current_step": "inspect",
            "completed_steps": [],
            "blockers": [],
            "next_action": "read tests",
            "requires_approval": False,
            "acceptance_checks": [
                {
                    "text": "The targeted test passes",
                    "source": "Fix the failing targeted test",
                    "verification_command": "python -m unittest tests.test_target",
                }
            ],
        }
        args.update(overrides)
        return args

    def test_start_assigns_stable_ids_and_revision(self):
        state = AgentRuntimeState(session_id="acceptance-start")

        result = execute_tool_result(
            "update_plan_state",
            self._args(),
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertEqual(result.status, "success")
        acceptance = result.metadata["planning_state"]["acceptance"]
        self.assertEqual(acceptance["revision"], 1)
        self.assertEqual(
            acceptance["checks"],
            [
                {
                    "id": "check_1",
                    "text": "The targeted test passes",
                    "source": "Fix the failing targeted test",
                    "verification_command": "python -m unittest tests.test_target",
                    "origin": "main_agent",
                }
            ],
        )

    def test_progress_applies_atomic_acceptance_operations(self):
        state = AgentRuntimeState(session_id="acceptance-progress")
        execute_tool_result(
            "update_plan_state",
            self._args(),
            runtime_state=state,
            agent_name="main_agent",
        )

        result = execute_tool_result(
            "update_plan_state",
            self._args(
                update_kind="progress",
                current_step="verify",
                next_action="run revised verification",
                acceptance_revision=1,
                acceptance_operations=[
                    {
                        "operation": "update",
                        "id": "check_1",
                        "verification_command": "python -m pytest tests/test_target.py",
                        "reason": "The repository uses pytest for this test",
                    },
                    {
                        "operation": "add",
                        "text": "No unrelated files changed",
                        "source": "Keep the change scoped",
                        "verification_command": "git diff --stat",
                        "reason": "Repository inspection exposed a scope constraint",
                    },
                ],
            ),
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertEqual(result.status, "success")
        acceptance = result.metadata["planning_state"]["acceptance"]
        self.assertEqual(acceptance["revision"], 2)
        self.assertEqual([item["id"] for item in acceptance["checks"]], ["check_1", "check_2"])
        self.assertEqual(
            acceptance["checks"][0]["verification_command"],
            "python -m pytest tests/test_target.py",
        )

    def test_stale_revision_rejects_entire_operation_batch(self):
        state = AgentRuntimeState(session_id="acceptance-stale")
        execute_tool_result(
            "update_plan_state",
            self._args(),
            runtime_state=state,
            agent_name="main_agent",
        )

        result = execute_tool_result(
            "update_plan_state",
            self._args(
                update_kind="progress",
                acceptance_revision=0,
                acceptance_operations=[
                    {
                        "operation": "remove",
                        "id": "check_1",
                        "reason": "No longer applicable",
                    }
                ],
            ),
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertEqual(result.status, "failed")
        self.assertIn("stale acceptance_revision", result.output)
        self.assertEqual(
            [item["id"] for item in state.task_board.acceptance.snapshot()["checks"]],
            ["check_1"],
        )

    def test_async_review_is_merged_before_first_edit_and_notified_once(self):
        reviewer = Mock(
            return_value={
                "changes": [
                    {
                        "operation": "add",
                        "text": "The exact output format is preserved",
                        "source": "Preserve the exact output format",
                        "verification_command": "python -m unittest tests.test_format",
                        "reason": "The initial checks missed an explicit output constraint",
                    }
                ]
            }
        )
        middleware = AcceptanceReviewMiddleware(reviewer=reviewer, timeout_seconds=0.5)
        state = AgentRuntimeState(session_id="acceptance-review")
        state.task_board.original_task = "Fix the test and preserve the exact output format"
        execute_tool_result(
            "update_plan_state",
            self._args(),
            runtime_state=state,
            agent_name="main_agent",
        )

        middleware.post_tool(
            "update_plan_state",
            self._args(),
            "Updated plan state",
            messages=[],
            runtime_state=state,
            agent_name="main_agent",
        )
        blocked = middleware.before_tool(
            "write_file",
            {"path": "target.py", "content": "fixed"},
            messages=[],
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertIsNone(blocked)
        snapshot = state.task_board.acceptance.snapshot()
        self.assertEqual(snapshot["review_status"], "completed")
        self.assertEqual(snapshot["revision"], 2)
        self.assertEqual(len(snapshot["checks"]), 2)
        notice = middleware.post_tool(
            "read_file",
            {"path": "target.py"},
            "contents",
            messages=[],
            runtime_state=state,
            agent_name="main_agent",
        )
        self.assertIn("Fast acceptance review updated", notice)
        self.assertIn("check_2", notice)
        self.assertIsNone(
            middleware.post_tool(
                "read_file",
                {"path": "target.py"},
                "contents",
                messages=[],
                runtime_state=state,
                agent_name="main_agent",
            )
        )

    def test_review_failure_retries_once_then_fails_open(self):
        reviewer = Mock(side_effect=TimeoutError("slow"))
        middleware = AcceptanceReviewMiddleware(reviewer=reviewer, timeout_seconds=0.01)
        state = AgentRuntimeState(session_id="acceptance-review-failure")
        state.task_board.original_task = "Fix the task"
        execute_tool_result(
            "update_plan_state",
            self._args(),
            runtime_state=state,
            agent_name="main_agent",
        )

        middleware.post_tool(
            "update_plan_state",
            self._args(),
            "Updated plan state",
            messages=[],
            runtime_state=state,
            agent_name="main_agent",
        )
        self.assertIsNone(
            middleware.before_tool(
                "apply_patch",
                {"path": "target.py", "search": "bad", "replace": "good"},
                messages=[],
                runtime_state=state,
                agent_name="main_agent",
            )
        )

        snapshot = state.task_board.acceptance.snapshot()
        self.assertEqual(reviewer.call_count, 2)
        self.assertEqual(snapshot["review_status"], "failed_open")
        self.assertIn("slow", snapshot["review_warning"])

    def test_review_usage_is_emitted_for_eval_cost_accounting(self):
        reviewer = Mock(
            return_value=ReviewOutcome(
                raw='{"changes": []}',
                usage={"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
                provider="test-provider",
                model="test-fast",
            )
        )
        middleware = AcceptanceReviewMiddleware(reviewer=reviewer, timeout_seconds=0.5)
        state = AgentRuntimeState(session_id="acceptance-review-usage", event_bus=EventBus())
        state.task_board.original_task = "Fix the task"
        execute_tool_result(
            "update_plan_state",
            self._args(),
            runtime_state=state,
            agent_name="main_agent",
        )

        middleware.post_tool(
            "update_plan_state",
            self._args(),
            "Updated plan state",
            messages=[],
            runtime_state=state,
            agent_name="main_agent",
        )
        middleware.before_tool(
            "write_file",
            {"path": "target.py", "content": "fixed"},
            messages=[],
            runtime_state=state,
            agent_name="main_agent",
        )

        usage_events = [event for event in state.event_bus.events if event.type == "llm_usage"]
        self.assertEqual(len(usage_events), 1)
        self.assertEqual(usage_events[0].payload["purpose"], "acceptance_review")
        self.assertEqual(usage_events[0].payload["total_tokens"], 25)

    def test_success_requires_current_revision_and_all_checks_passed(self):
        state = AgentRuntimeState(session_id="acceptance-final-success")
        execute_tool_result(
            "update_plan_state",
            self._args(),
            runtime_state=state,
            agent_name="main_agent",
        )

        result = execute_tool_result(
            "update_plan_state",
            self._args(
                update_kind="final",
                next_action="none",
                result_status="success",
                validation="targeted test passed",
                remaining_issues=[],
                acceptance_revision=1,
                check_results=[
                    {
                        "id": "check_1",
                        "status": "passed",
                        "summary": "The targeted unittest exited with code 0",
                    }
                ],
            ),
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertEqual(result.status, "success")
        self.assertIn('"check_results"', result.output)

    def test_success_rejects_stale_revision(self):
        state = AgentRuntimeState(session_id="acceptance-final-stale")
        execute_tool_result(
            "update_plan_state",
            self._args(),
            runtime_state=state,
            agent_name="main_agent",
        )

        result = execute_tool_result(
            "update_plan_state",
            self._args(
                update_kind="final",
                next_action="none",
                result_status="success",
                validation="targeted test passed",
                remaining_issues=[],
                acceptance_revision=0,
                check_results=[
                    {
                        "id": "check_1",
                        "status": "passed",
                        "summary": "The targeted unittest exited with code 0",
                    }
                ],
            ),
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertEqual(result.status, "failed")
        self.assertIn("stale acceptance_revision", result.output)
        self.assertIn('"check_1"', result.output)

    def test_failed_exit_accepts_stale_results_and_fills_latest_checks(self):
        state = AgentRuntimeState(session_id="acceptance-final-failed")
        execute_tool_result(
            "update_plan_state",
            self._args(),
            runtime_state=state,
            agent_name="main_agent",
        )
        execute_tool_result(
            "update_plan_state",
            self._args(
                update_kind="progress",
                acceptance_revision=1,
                acceptance_operations=[
                    {
                        "operation": "add",
                        "text": "No regression",
                        "source": "Avoid regressions",
                        "verification_command": "pytest",
                        "reason": "Added after inspection",
                    }
                ],
            ),
            runtime_state=state,
            agent_name="main_agent",
        )

        result = execute_tool_result(
            "update_plan_state",
            self._args(
                update_kind="final",
                next_action="none",
                result_status="failed",
                validation="verification could not complete",
                remaining_issues=["targeted test still fails"],
                acceptance_revision=1,
                check_results=[
                    {
                        "id": "removed_or_unknown",
                        "status": "failed",
                        "summary": "Old result",
                    }
                ],
            ),
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertEqual(result.status, "success")
        acceptance = result.metadata["planning_state"]["acceptance"]
        self.assertTrue(acceptance["stale_final_submission"])
        self.assertEqual(
            [item["status"] for item in acceptance["check_results"]],
            ["not_run", "not_run"],
        )

    def test_state_write_failure_rolls_back_acceptance_mutation(self):
        state = AgentRuntimeState(session_id="acceptance-write-failure")

        with patch("harness_code_agent.runtime.builtins.planning.os.replace", side_effect=OSError("locked")):
            result = execute_tool_result(
                "update_plan_state",
                self._args(),
                runtime_state=state,
                agent_name="main_agent",
            )

        self.assertEqual(result.status, "failed")
        self.assertEqual(state.task_board.acceptance.snapshot()["revision"], 0)
        self.assertEqual(state.task_board.acceptance.snapshot()["checks"], [])


if __name__ == "__main__":
    unittest.main()
