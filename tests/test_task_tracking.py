import json
import os
import shutil
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


def _install_fake_openai_module() -> None:
    openai = types.ModuleType("openai")

    class OpenAI:
        def __init__(self, *args, **kwargs):
            pass

    openai.OpenAI = OpenAI
    sys.modules["openai"] = openai


_install_fake_openai_module()

from harness_code_agent import config
from harness_code_agent.agent.conversation import AgentRuntimeState
from harness_code_agent.runtime.middlewares import (
    RecoveryStrategyMiddleware,
    TaskTrackingEnforcementMiddleware,
)
from harness_code_agent.runtime.tool_result import ToolResult
from harness_code_agent.runtime.tools import execute_tool, execute_tool_result


def _result(text: str, *, status: str | None = None) -> ToolResult:
    """Build a ToolResult from the legacy text conventions used in these tests."""
    if status is None:
        status = "failed" if text.startswith(("[error]", "[blocked]")) else "success"
    metadata = {"status_source": "permission"} if text.startswith("[blocked]") else {}
    error = text.removeprefix("[error] ").removeprefix("[blocked] ") if status == "failed" else None
    return ToolResult(tool="run_bash", status=status, output=text, error=error, metadata=metadata)



class UpdatePlanStateToolTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = os.path.join(os.getcwd(), "workspace", "test-planning-tool")
        self.old_workspace = config.WORKSPACE
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        os.makedirs(self.temp_dir, exist_ok=True)
        config.WORKSPACE = self.temp_dir

    def tearDown(self):
        config.WORKSPACE = self.old_workspace
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _state_path(self, session_id: str = "test-session") -> Path:
        return Path(self.temp_dir, ".harness", "sessions", session_id, "planning", "state.json")

    def _base_args(self, **overrides):
        args = {
            "mode": "tracked",
            "update_kind": "start",
            "goal": "fix task",
            "steps": ["inspect", "edit", "verify"],
            "current_step": "inspect",
            "completed_steps": [],
            "blockers": [],
            "next_action": "read tests",
            "requires_approval": False,
        }
        args.update(overrides)
        return args

    def test_tracked_mode_writes_only_session_state_json(self):
        state = AgentRuntimeState(session_id="test-session")
        result = execute_tool(
            "update_plan_state",
            self._base_args(),
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertIn("Updated plan state", result)
        self.assertEqual(state.task_board.planning_mode, "tracked")
        self.assertEqual(state.task_board.current_step, "inspect")
        self.assertEqual(state.task_board.update_count, 1)
        state_path = self._state_path()
        self.assertTrue(state_path.exists())
        data = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(data["mode"], "tracked")
        self.assertEqual(data["update_kind"], "start")
        self.assertFalse(data["requires_approval"])
        self.assertFalse(Path(self.temp_dir, "global_plan", "current", "plan.md").exists())

    def test_todo_mode_writes_lightweight_state_without_acceptance(self):
        state = AgentRuntimeState(session_id="todo-session")
        result = execute_tool_result(
            "update_plan_state",
            self._base_args(mode="todo", steps=["write", "test"], current_step="write"),
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertEqual(result.status, "success")
        self.assertEqual(state.task_board.planning_mode, "todo")
        self.assertEqual(state.task_board.acceptance.revision, 0)
        data = json.loads(self._state_path("todo-session").read_text(encoding="utf-8"))
        self.assertEqual(data["mode"], "todo")

    def test_todo_progress_ignores_tracked_replan_flag(self):
        state = AgentRuntimeState(session_id="todo-recovery")
        state.task_board.replan_required = True
        state.task_board.replan_reason = "previous check failed"

        result = execute_tool_result(
            "update_plan_state",
            self._base_args(mode="todo", update_kind="progress"),
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertEqual(result.status, "success")
        self.assertEqual(state.task_board.planning_mode, "todo")

    def test_plan_state_tool_result_exposes_planning_state_metadata(self):
        state = AgentRuntimeState(session_id="test-session")

        result = execute_tool_result(
            "update_plan_state",
            self._base_args(
                current_step="edit",
                completed_steps=["inspect"],
                next_action="patch widgets",
            ),
            runtime_state=state,
            agent_name="main_agent",
        )

        planning_state = result.metadata.get("planning_state")
        self.assertIsNotNone(planning_state)
        self.assertEqual(planning_state["steps"], ["inspect", "edit", "verify"])
        self.assertEqual(planning_state["current_step"], "edit")
        self.assertEqual(planning_state["completed_steps"], ["inspect"])

    def test_legacy_full_mode_is_rejected(self):
        state = AgentRuntimeState(session_id="test-session")
        result = execute_tool(
            "update_plan_state",
            self._base_args(
                mode="full",
                requires_approval=True,
                plan_markdown="# Plan\n\n- inspect\n",
            ),
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertIn("[error]", result)
        self.assertIn("mode must be one of: todo, tracked", result)
        self.assertFalse(self._state_path().exists())
        self.assertFalse(Path(self.temp_dir, "global_plan").exists())

    def test_skip_mode_is_not_a_tool_mode_and_writes_nothing(self):
        state = AgentRuntimeState(session_id="test-session")
        result = execute_tool(
            "update_plan_state",
            self._base_args(mode="skip"),
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertIn("[error]", result)
        self.assertIn("mode must be one of: todo, tracked", result)
        self.assertFalse(self._state_path().exists())
        self.assertFalse(Path(self.temp_dir, "global_plan").exists())

    def test_final_requires_result_validation_and_remaining_issues(self):
        state = AgentRuntimeState(session_id="test-session")
        result = execute_tool(
            "update_plan_state",
            self._base_args(update_kind="final", next_action="none"),
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertIn("[error]", result)
        self.assertIn("final update requires result_status", result)
        self.assertFalse(self._state_path().exists())

    def test_current_step_must_be_declared(self):
        state = AgentRuntimeState(session_id="test-session")

        result = execute_tool(
            "update_plan_state",
            self._base_args(current_step="invented"),
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertIn("[error]", result)
        self.assertIn("current_step must be one of the declared steps", result)
        self.assertFalse(self._state_path().exists())

    def test_completed_steps_must_be_declared(self):
        state = AgentRuntimeState(session_id="test-session")

        result = execute_tool(
            "update_plan_state",
            self._base_args(completed_steps=["invented"]),
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertIn("[error]", result)
        self.assertIn("completed_steps must be a subset of steps", result)
        self.assertFalse(self._state_path().exists())

    def test_replan_keeps_completed_steps_from_previous_plan(self):
        state = AgentRuntimeState(session_id="test-session")

        result = execute_tool_result(
            "update_plan_state",
            self._base_args(
                update_kind="replan",
                replan_reason="previous strategy failed",
                steps=["new inspect", "new verify"],
                current_step="new inspect",
                completed_steps=["old inspect", "old edit"],
                next_action="inspect the failing output",
            ),
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertEqual(result.status, "success")
        planning_state = result.metadata["planning_state"]
        self.assertEqual(
            planning_state["steps"],
            ["old inspect", "old edit", "new inspect", "new verify"],
        )
        self.assertEqual(planning_state["completed_steps"], ["old inspect", "old edit"])
        self.assertIn("Replan steps normalized", result.output)

    def test_plan_markdown_is_ignored_and_does_not_overwrite_formal_plan(self):
        state = AgentRuntimeState(session_id="test-session")
        plan_path = Path(self.temp_dir, "global_plan", "current", "plan.md")
        plan_path.parent.mkdir(parents=True)
        plan_path.write_text("# Existing\n", encoding="utf-8")

        result = execute_tool(
            "update_plan_state",
            self._base_args(
                update_kind="replan",
                replan_reason="technical test order changed",
                current_step="edit",
                completed_steps=["inspect"],
                next_action="run targeted test",
                requires_approval=True,
                plan_markdown="# Should not be written\n",
            ),
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertIn("Updated plan state", result)
        self.assertEqual(plan_path.read_text(encoding="utf-8"), "# Existing\n")
        self.assertFalse(state.task_board.requires_approval)
        self.assertFalse(state.task_board.replan_required)

    def test_progress_cannot_clear_required_replan(self):
        state = AgentRuntimeState(session_id="test-session")
        state.task_board.planning_mode = "tracked"
        state.task_board.replan_required = True
        state.task_board.replan_reason = "same verification failed twice"

        result = execute_tool(
            "update_plan_state",
            self._base_args(
                update_kind="progress",
                current_step="edit",
                completed_steps=["inspect"],
                next_action="retry the same edit",
            ),
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertIn("[error]", result)
        self.assertIn("replan", result.lower())
        self.assertTrue(state.task_board.replan_required)
        self.assertEqual(state.task_board.replan_reason, "same verification failed twice")
        self.assertFalse(self._state_path().exists())

    def test_required_replan_enters_probe_mode(self):
        state = AgentRuntimeState(session_id="test-session")
        state.recovery.mode = "SPEC_RECHECK"
        state.task_board.planning_mode = "tracked"
        state.task_board.replan_required = True
        state.task_board.replan_reason = "same verification failed twice"

        result = execute_tool(
            "update_plan_state",
            self._base_args(
                update_kind="replan",
                replan_reason="replace the faulty assumption with a targeted check",
                current_step="verify",
                completed_steps=["inspect", "edit"],
                next_action="run the targeted regression test",
            ),
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertIn("Updated plan state", result)
        self.assertFalse(state.task_board.replan_required)
        self.assertEqual(state.recovery.mode, "PROBE")

    def test_required_replan_accepts_start_as_replan(self):
        state = AgentRuntimeState(session_id="test-session")
        state.recovery.mode = "SPEC_RECHECK"
        state.recovery.failure_signature = "same verification failed twice"
        state.task_board.planning_mode = "tracked"
        state.task_board.replan_required = True
        state.task_board.replan_reason = "same verification failed twice"

        result = execute_tool(
            "update_plan_state",
            self._base_args(
                update_kind="start",
                current_step="verify",
                completed_steps=["inspect", "edit"],
                next_action="run the targeted regression test",
            ),
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertIn("Updated plan state", result)
        self.assertFalse(state.task_board.replan_required)
        self.assertEqual(state.task_board.replan_reason, "same verification failed twice")
        self.assertEqual(state.recovery.mode, "PROBE")
        data = json.loads(self._state_path().read_text(encoding="utf-8"))
        self.assertEqual(data["update_kind"], "replan")
        self.assertEqual(data["replan_reason"], "same verification failed twice")

    def test_required_replan_defaults_missing_reason_from_recovery_state(self):
        state = AgentRuntimeState(session_id="test-session")
        state.recovery.mode = "SPEC_RECHECK"
        state.recovery.failure_signature = "probe failed after replan"
        state.task_board.planning_mode = "tracked"
        state.task_board.replan_required = True
        state.task_board.replan_reason = ""

        result = execute_tool(
            "update_plan_state",
            self._base_args(
                update_kind="replan",
                current_step="verify",
                completed_steps=["inspect", "edit"],
                next_action="run the targeted regression test",
            ),
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertIn("Updated plan state", result)
        self.assertFalse(state.task_board.replan_required)
        self.assertEqual(state.task_board.replan_reason, "probe failed after replan")
        self.assertEqual(state.recovery.mode, "PROBE")

    def test_repeated_failed_required_replans_trigger_fallback(self):
        state = AgentRuntimeState(session_id="test-session")
        state.task_board.planning_mode = "tracked"
        state.task_board.replan_required = True
        state.task_board.replan_reason = "same verification failed twice"
        middleware = RecoveryStrategyMiddleware()

        for _ in range(3):
            middleware.post_tool(
                "update_plan_state",
                {"update_kind": "replan"},
                _result("[error] replan update requires replan_reason"),
                [],
                runtime_state=state,
                agent_name="main_agent",
            )

        self.assertTrue(state.fallback.stop_requested)
        self.assertEqual(state.fallback.stop_reason, "replan_deadlock")
        self.assertTrue(state.task_board.replan_required)

    def test_atomic_replace_failure_keeps_previous_state_json(self):
        state = AgentRuntimeState(session_id="test-session")
        state_path = self._state_path()
        state_path.parent.mkdir(parents=True)
        state_path.write_text('{"mode": "old"}\n', encoding="utf-8")

        with patch("harness_code_agent.runtime.builtins.planning.os.replace", side_effect=OSError("locked")):
            result = execute_tool(
                "update_plan_state",
                self._base_args(),
                runtime_state=state,
                agent_name="main_agent",
            )

        self.assertIn("[error]", result)
        self.assertEqual(json.loads(state_path.read_text(encoding="utf-8")), {"mode": "old"})
        self.assertEqual(list(state_path.parent.glob("state.json.tmp.*")), [])

class TaskTrackingEnforcementTests(unittest.TestCase):
    def test_unset_mode_allows_first_action_for_skip_path(self):
        middleware = TaskTrackingEnforcementMiddleware()
        state = AgentRuntimeState()

        blocked = middleware.before_tool(
            "run_bash",
            {"command": "pwd"},
            messages=[],
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertIsNone(blocked)

    def test_tracked_mode_allows_action_when_start_missing_and_reminds_once(self):
        middleware = TaskTrackingEnforcementMiddleware()
        state = AgentRuntimeState()
        state.task_board.planning_mode = "tracked"

        blocked = middleware.before_tool(
            "run_bash",
            {"command": "pytest"},
            messages=[],
            runtime_state=state,
            agent_name="main_agent",
        )
        early_post = []
        for _ in range(4):
            early_post.append(
                middleware.post_tool(
                    "run_bash",
                    {"command": "pytest"},
                    _result("ok"),
                    messages=[],
                    runtime_state=state,
                    agent_name="main_agent",
                )
            )
        reminder = middleware.post_tool(
            "run_bash",
            {"command": "pytest"},
            _result("ok"),
            messages=[],
            runtime_state=state,
            agent_name="main_agent",
        )
        second_reminder = middleware.post_tool(
            "run_bash",
            {"command": "pytest"},
            _result("ok"),
            messages=[],
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertIsNone(blocked)
        self.assertTrue(all(post is None for post in early_post))
        self.assertIn("update_plan_state", reminder)
        self.assertIn("start", reminder)
        self.assertIsNone(second_reminder)

    def test_tracked_mode_allows_read_only_probe_before_start(self):
        middleware = TaskTrackingEnforcementMiddleware()
        state = AgentRuntimeState()
        state.task_board.planning_mode = "tracked"

        blocked = middleware.before_tool(
            "run_bash",
            {"command": "ls -la /app 2>/dev/null; which python3"},
            messages=[],
            runtime_state=state,
            agent_name="main_agent",
        )
        reminder = middleware.post_tool(
            "run_bash",
            {"command": "ls -la /app 2>/dev/null; which python3"},
            _result("ok"),
            messages=[],
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertIsNone(blocked)
        self.assertIsNone(reminder)
        self.assertEqual(state.action_tool_count, 0)
        self.assertEqual(state.task_board.action_count, 0)
        self.assertFalse(state.task_board.needs_final_update)

    def test_tracked_mode_counts_verification_command_before_start(self):
        middleware = TaskTrackingEnforcementMiddleware()
        state = AgentRuntimeState()
        state.task_board.planning_mode = "tracked"

        blocked = middleware.before_tool(
            "run_bash",
            {"command": "pytest"},
            messages=[],
            runtime_state=state,
            agent_name="main_agent",
        )

        middleware.post_tool(
            "run_bash",
            {"command": "pytest"},
            _result("ok"),
            messages=[],
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertIsNone(blocked)
        self.assertEqual(state.task_board.action_count, 1)
        self.assertTrue(state.task_board.needs_final_update)

    def test_requires_approval_flag_does_not_block_tracked_actions(self):
        middleware = TaskTrackingEnforcementMiddleware()
        state = AgentRuntimeState()
        state.task_board.planning_mode = "tracked"
        state.task_board.update_count = 1
        state.task_board.requires_approval = True

        blocked = middleware.before_tool(
            "apply_patch",
            {"path": "x.py", "search": "a", "replace": "b"},
            messages=[],
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertIsNone(blocked)

    def test_replan_required_blocks_until_replan_update(self):
        middleware = TaskTrackingEnforcementMiddleware()
        state = AgentRuntimeState()
        state.task_board.planning_mode = "tracked"
        state.task_board.update_count = 1
        state.task_board.replan_required = True
        state.task_board.replan_reason = "same failure repeated"

        blocked = middleware.before_tool(
            "write_file",
            {"path": "x.py", "content": "x"},
            messages=[],
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertIsNotNone(blocked)
        self.assertIn("replan", blocked.lower())
        self.assertIn("update_plan_state", blocked)

    def test_progress_is_not_forced_by_action_count(self):
        middleware = TaskTrackingEnforcementMiddleware()
        state = AgentRuntimeState()
        state.task_board.planning_mode = "tracked"
        state.task_board.update_count = 1

        for _ in range(3):
            self.assertIsNone(
                middleware.before_tool(
                    "run_bash",
                    {"command": "pytest"},
                    messages=[],
                    runtime_state=state,
                    agent_name="main_agent",
                )
            )
            reminder = middleware.post_tool(
                "run_bash",
                {"command": "pytest"},
                _result("ok"),
                messages=[],
                runtime_state=state,
                agent_name="main_agent",
            )

        self.assertIsNone(reminder)

    def test_tracked_mode_requires_final_planning_update_after_action(self):
        middleware = TaskTrackingEnforcementMiddleware()
        state = AgentRuntimeState()
        state.task_board.planning_mode = "tracked"
        state.task_board.update_count = 1
        state.task_board.needs_final_update = True

        blocked = middleware.pre_exit(messages=[], runtime_state=state, agent_name="main_agent")

        self.assertIsNotNone(blocked)
        self.assertIn("update_plan_state", blocked)
        self.assertIn("final", blocked)

    def test_tracked_start_requires_acceptance_checks(self):
        middleware = TaskTrackingEnforcementMiddleware(enforce_acceptance=True)
        state = AgentRuntimeState()
        state.task_board.planning_mode = "tracked"

        blocked = middleware.before_tool(
            "update_plan_state",
            {"update_kind": "start", "acceptance_checks": []},
            messages=[],
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertIsNotNone(blocked)
        self.assertIn("acceptance_checks", blocked)
        self.assertNotIn("Terminal", blocked)

    def test_todo_mode_has_no_acceptance_or_replan_gate(self):
        middleware = TaskTrackingEnforcementMiddleware(enforce_acceptance=True)
        state = AgentRuntimeState()
        state.task_board.planning_mode = "todo"
        state.task_board.replan_required = True
        state.task_board.replan_reason = "a verification failed"

        final_update = middleware.before_tool(
            "update_plan_state",
            {"mode": "todo", "update_kind": "final", "acceptance_checks": []},
            messages=[],
            runtime_state=state,
            agent_name="main_agent",
        )
        next_action = middleware.before_tool(
            "run_bash",
            {"command": "python -m unittest"},
            messages=[],
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertIsNone(final_update)
        self.assertIsNone(next_action)

    def test_unset_mode_allows_small_skip_path_before_start_threshold(self):
        middleware = TaskTrackingEnforcementMiddleware(
            enforce_acceptance=True,
            require_start_after_n_actions=3,
        )
        state = AgentRuntimeState()

        for _ in range(2):
            self.assertIsNone(
                middleware.before_tool(
                    "run_bash",
                    {"command": "pytest"},
                    messages=[],
                    runtime_state=state,
                    agent_name="main_agent",
                )
            )
            self.assertIsNone(
                middleware.post_tool(
                    "run_bash",
                    {"command": "pytest"},
                    _result("ok"),
                    messages=[],
                    runtime_state=state,
                    agent_name="main_agent",
                )
            )

        self.assertEqual(state.task_board.planning_mode, "unset")
        self.assertEqual(state.task_board.action_count, 2)

    def test_unset_mode_requires_tracked_start_after_action_threshold(self):
        middleware = TaskTrackingEnforcementMiddleware(
            enforce_acceptance=True,
            require_start_after_n_actions=2,
        )
        state = AgentRuntimeState()

        for _ in range(2):
            self.assertIsNone(
                middleware.before_tool(
                    "run_bash",
                    {"command": "pytest"},
                    messages=[],
                    runtime_state=state,
                    agent_name="main_agent",
                )
            )
            middleware.post_tool(
                "run_bash",
                {"command": "pytest"},
                _result("ok"),
                messages=[],
                runtime_state=state,
                agent_name="main_agent",
            )

        blocked = middleware.before_tool(
            "apply_patch",
            {"path": "x.py", "search": "a", "replace": "b"},
            messages=[],
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertIsNotNone(blocked)
        self.assertIn("multi-step execution", blocked)
        self.assertIn("update_plan_state", blocked)
        self.assertIn("acceptance_checks", blocked)

    def test_pre_start_read_only_delegate_is_allowed_after_action_threshold(self):
        middleware = TaskTrackingEnforcementMiddleware(
            enforce_acceptance=True,
            require_start_after_n_actions=2,
        )
        state = AgentRuntimeState()

        for _ in range(2):
            middleware.post_tool(
                "run_bash",
                {"command": "pytest"},
                _result("ok"),
                messages=[],
                runtime_state=state,
                agent_name="main_agent",
            )

        blocked = middleware.before_tool(
            "delegate_agent",
            {"agent_profile": "explore", "task": "inspect the parser"},
            messages=[],
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertIsNone(blocked)

    def test_pre_start_read_only_delegate_does_not_count_as_tracked_action(self):
        middleware = TaskTrackingEnforcementMiddleware()
        state = AgentRuntimeState()
        state.task_board.planning_mode = "tracked"

        reminder = middleware.post_tool(
            "delegate_agent",
            {"agent_profile": "explore", "task": "inspect the parser"},
            _result("ok"),
            messages=[],
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertIsNone(reminder)
        self.assertEqual(state.action_tool_count, 0)
        self.assertEqual(state.task_board.action_count, 0)
        self.assertFalse(state.task_board.needs_final_update)

    def test_pre_start_patch_delegate_requires_tracked_start_after_threshold(self):
        middleware = TaskTrackingEnforcementMiddleware(
            enforce_acceptance=True,
            require_start_after_n_actions=2,
        )
        state = AgentRuntimeState()

        for _ in range(2):
            middleware.post_tool(
                "run_bash",
                {"command": "pytest"},
                _result("ok"),
                messages=[],
                runtime_state=state,
                agent_name="main_agent",
            )

        blocked = middleware.before_tool(
            "delegate_agent",
            {"agent_profile": "patch", "task": "draft a parser fix"},
            messages=[],
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertIsNotNone(blocked)
        self.assertIn("update_plan_state", blocked)

    def test_threshold_start_with_acceptance_checks_allows_more_actions(self):
        middleware = TaskTrackingEnforcementMiddleware(
            enforce_acceptance=True,
            require_start_after_n_actions=1,
        )
        state = AgentRuntimeState()
        middleware.post_tool(
            "run_bash",
            {"command": "pytest"},
            _result("ok"),
            messages=[],
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertIsNone(
            middleware.before_tool(
                "update_plan_state",
                {
                    "update_kind": "start",
                    "acceptance_checks": [
                        {
                            "text": "Tests pass",
                            "source": "User asked for verified fix",
                            "verification_command": "pytest",
                        }
                    ],
                },
                messages=[],
                runtime_state=state,
                agent_name="main_agent",
            )
        )
        state.task_board.planning_mode = "tracked"
        state.task_board.update_count = 1

        self.assertIsNone(
            middleware.before_tool(
                "apply_patch",
                {"path": "x.py", "search": "a", "replace": "b"},
                messages=[],
                runtime_state=state,
                agent_name="main_agent",
            )
        )

    def test_acceptance_success_requires_check_results(self):
        middleware = TaskTrackingEnforcementMiddleware(enforce_acceptance=True)
        state = AgentRuntimeState()
        state.task_board.planning_mode = "tracked"
        state.task_board.update_count = 1
        state.task_board.acceptance.initialize(
            [{"text": "Tests pass", "source": "Run the tests", "verification_command": "pytest"}]
        )
        state.execution_facts.record_result(
            "run_bash",
            status="success",
            return_code=0,
            metadata={"status_source": "shell"},
        )

        blocked = middleware.before_tool(
            "update_plan_state",
            {
                "update_kind": "final",
                "result_status": "success",
                "acceptance_revision": 1,
                "check_results": [],
            },
            messages=[],
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertIsNotNone(blocked)
        self.assertIn("every active acceptance check", blocked)

    def test_terminal_success_requires_latest_foreground_shell_to_succeed_after_edit(self):
        middleware = TaskTrackingEnforcementMiddleware(enforce_acceptance=True)
        state = AgentRuntimeState()
        state.task_board.planning_mode = "tracked"
        state.task_board.update_count = 1
        state.task_board.acceptance.initialize(
            [{"text": "Tests pass", "source": "Run the tests", "verification_command": "pytest"}]
        )
        state.execution_facts.record_result(
            "write_file",
            status="success",
            return_code=None,
            metadata={"file_changes": [{"path": "app.py"}]},
        )
        state.execution_facts.record_result(
            "run_bash",
            status="success",
            return_code=0,
            metadata={"status_source": "shell"},
        )
        state.execution_facts.record_result(
            "run_bash",
            status="failed",
            return_code=1,
            metadata={"status_source": "shell"},
        )

        blocked = middleware.before_tool(
            "update_plan_state",
            {
                "update_kind": "final",
                "result_status": "success",
                "acceptance_revision": 1,
                "check_results": [
                    {"id": "check_1", "status": "passed", "summary": "pytest passed"}
                ],
            },
            messages=[],
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertIsNotNone(blocked)
        self.assertIn("last foreground run_bash", blocked)

    def test_terminal_success_requires_shell_after_last_edit(self):
        middleware = TaskTrackingEnforcementMiddleware(enforce_acceptance=True)
        state = AgentRuntimeState()
        state.task_board.planning_mode = "tracked"
        state.task_board.update_count = 1
        state.task_board.acceptance.initialize(
            [{"text": "Tests pass", "source": "Run the tests", "verification_command": "pytest"}]
        )
        state.execution_facts.record_result(
            "run_bash",
            status="success",
            return_code=0,
            metadata={"status_source": "shell"},
        )
        state.execution_facts.record_result(
            "apply_patch",
            status="success",
            return_code=None,
            metadata={"file_changes": [{"path": "app.py"}]},
        )

        blocked = middleware.before_tool(
            "update_plan_state",
            {
                "update_kind": "final",
                "result_status": "success",
                "acceptance_revision": 1,
                "check_results": [
                    {"id": "check_1", "status": "passed", "summary": "pytest passed"}
                ],
            },
            messages=[],
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertIsNotNone(blocked)
        self.assertIn("after the last business file edit", blocked)

    def test_terminal_timeout_counts_as_failed_last_foreground_shell(self):
        middleware = TaskTrackingEnforcementMiddleware(enforce_acceptance=True)
        state = AgentRuntimeState()
        state.task_board.planning_mode = "tracked"
        state.task_board.update_count = 1
        state.task_board.acceptance.initialize(
            [{"text": "Tests pass", "source": "Run the tests", "verification_command": "pytest"}]
        )
        state.execution_facts.record_result(
            "run_bash",
            status="success",
            return_code=0,
            metadata={"status_source": "shell"},
        )
        state.execution_facts.record_result(
            "run_bash",
            status="failed",
            return_code=None,
            metadata={"status_source": "shell", "timed_out": True},
        )

        blocked = middleware.before_tool(
            "update_plan_state",
            {
                "update_kind": "final",
                "result_status": "success",
                "acceptance_revision": 1,
                "check_results": [
                    {"id": "check_1", "status": "passed", "summary": "pytest passed"}
                ],
            },
            messages=[],
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertIsNotNone(blocked)
        self.assertIn("last foreground run_bash", blocked)

    def test_terminal_success_rejects_design_assertion_verification_commands(self):
        middleware = TaskTrackingEnforcementMiddleware(enforce_acceptance=True)
        state = AgentRuntimeState()
        state.task_board.planning_mode = "tracked"
        state.task_board.update_count = 1
        state.task_board.acceptance.initialize(
            [
                {
                    "text": "Only semantic-preserving edits were made",
                    "source": "Task constraint",
                    "verification_command": "echo 'checked by design - only write_file used'",
                }
            ]
        )
        state.execution_facts.record_result(
            "write_file",
            status="success",
            return_code=None,
            metadata={"file_changes": [{"path": "input.tex"}]},
        )
        state.execution_facts.record_result(
            "run_bash",
            status="success",
            return_code=0,
            metadata={"status_source": "shell"},
        )

        blocked = middleware.before_tool(
            "update_plan_state",
            {
                "update_kind": "final",
                "result_status": "success",
                "acceptance_revision": 1,
                "check_results": [
                    {"id": "check_1", "status": "passed", "summary": "Checked by design"}
                ],
            },
            messages=[],
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertIsNotNone(blocked)
        self.assertIn("real verification command", blocked)

    def test_terminal_success_rejects_echo_check_manually_after_noop_command(self):
        middleware = TaskTrackingEnforcementMiddleware(enforce_acceptance=True)
        state = AgentRuntimeState()
        state.task_board.planning_mode = "tracked"
        state.task_board.update_count = 1
        state.task_board.acceptance.initialize(
            [
                {
                    "text": "Only input.tex was edited",
                    "source": "Task constraint",
                    "verification_command": "grep -c '' main.tex synonyms.txt > /dev/null; echo 'check manually'",
                }
            ]
        )
        state.execution_facts.record_result(
            "write_file",
            status="success",
            return_code=None,
            metadata={"file_changes": [{"path": "input.tex"}]},
        )
        state.execution_facts.record_result(
            "run_bash",
            status="success",
            return_code=0,
            metadata={"status_source": "shell"},
        )

        blocked = middleware.before_tool(
            "update_plan_state",
            {
                "update_kind": "final",
                "result_status": "success",
                "acceptance_revision": 1,
                "check_results": [
                    {"id": "check_1", "status": "passed", "summary": "Checked manually"}
                ],
            },
            messages=[],
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertIsNotNone(blocked)
        self.assertIn("real verification command", blocked)

if __name__ == "__main__":
    unittest.main()
