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
from harness_code_agent.agent.loop import AgentRuntimeState
from harness_code_agent.runtime.middlewares import TaskTrackingEnforcementMiddleware
from harness_code_agent.runtime.tools import execute_tool


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
            "mode": "light",
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

    def test_light_mode_writes_only_session_state_json(self):
        state = AgentRuntimeState(session_id="test-session")
        result = execute_tool(
            "update_plan_state",
            self._base_args(),
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertIn("Updated plan state", result)
        self.assertEqual(state.task_board.planning_mode, "light")
        self.assertEqual(state.task_board.current_step, "inspect")
        self.assertEqual(state.task_board.update_count, 1)
        state_path = self._state_path()
        self.assertTrue(state_path.exists())
        data = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(data["mode"], "light")
        self.assertEqual(data["update_kind"], "start")
        self.assertFalse(data["requires_approval"])
        self.assertFalse(Path(self.temp_dir, "global_plan", "current", "plan.md").exists())
        self.assertFalse(Path(self.temp_dir, config.PROGRESS_FILE).exists())
        self.assertFalse(Path(self.temp_dir, "task_plan.md").exists())
        self.assertFalse(Path(self.temp_dir, "findings.md").exists())

    def test_full_start_writes_state_and_plan_with_approval_required(self):
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

        self.assertIn("global_plan/current/plan.md", result.replace("\\", "/"))
        self.assertTrue(self._state_path().exists())
        plan_path = Path(self.temp_dir, "global_plan", "current", "plan.md")
        self.assertEqual(plan_path.read_text(encoding="utf-8"), "# Plan\n\n- inspect\n")
        self.assertTrue(state.task_board.requires_approval)
        self.assertEqual(state.task_board.plan_revision, 1)
        self.assertFalse(Path(self.temp_dir, "global_plan", "current", "status.md").exists())
        self.assertFalse(Path(self.temp_dir, "global_plan", "current", "final.md").exists())

    def test_skip_mode_is_not_a_tool_mode_and_writes_nothing(self):
        state = AgentRuntimeState(session_id="test-session")
        result = execute_tool(
            "update_plan_state",
            self._base_args(mode="skip"),
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertIn("[error]", result)
        self.assertIn("mode must be one of: light, full", result)
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

    def test_requires_approval_false_replan_does_not_overwrite_plan(self):
        state = AgentRuntimeState(session_id="test-session")
        plan_path = Path(self.temp_dir, "global_plan", "current", "plan.md")
        plan_path.parent.mkdir(parents=True)
        plan_path.write_text("# Existing\n", encoding="utf-8")

        result = execute_tool(
            "update_plan_state",
            self._base_args(
                mode="full",
                update_kind="replan",
                replan_reason="technical test order changed",
                current_step="edit",
                completed_steps=["inspect"],
                next_action="run targeted test",
                requires_approval=False,
                plan_markdown="# Should not be written\n",
            ),
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertIn("Updated plan state", result)
        self.assertEqual(plan_path.read_text(encoding="utf-8"), "# Existing\n")
        self.assertFalse(state.task_board.requires_approval)
        self.assertFalse(state.task_board.replan_required)

    def test_atomic_replace_failure_keeps_previous_state_json(self):
        state = AgentRuntimeState(session_id="test-session")
        state_path = self._state_path()
        state_path.parent.mkdir(parents=True)
        state_path.write_text('{"mode": "old"}\n', encoding="utf-8")

        with patch("harness_code_agent.runtime.tools.os.replace", side_effect=OSError("locked")):
            result = execute_tool(
                "update_plan_state",
                self._base_args(),
                runtime_state=state,
                agent_name="main_agent",
            )

        self.assertIn("[error]", result)
        self.assertEqual(json.loads(state_path.read_text(encoding="utf-8")), {"mode": "old"})
        self.assertEqual(list(state_path.parent.glob("state.json.tmp.*")), [])

    def test_old_tool_name_is_not_agent_callable(self):
        self.assertIn("Unknown tool", execute_tool("update_planning_files", {}, runtime_state=AgentRuntimeState()))


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

    def test_light_mode_blocks_when_start_missing(self):
        middleware = TaskTrackingEnforcementMiddleware()
        state = AgentRuntimeState()
        state.task_board.planning_mode = "light"

        blocked = middleware.before_tool(
            "run_bash",
            {"command": "pytest"},
            messages=[],
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertIsNotNone(blocked)
        self.assertIn("update_plan_state", blocked)
        self.assertIn("start", blocked)

    def test_requires_approval_blocks_action_until_cleared(self):
        middleware = TaskTrackingEnforcementMiddleware()
        state = AgentRuntimeState()
        state.task_board.planning_mode = "full"
        state.task_board.update_count = 1
        state.task_board.requires_approval = True

        blocked = middleware.before_tool(
            "apply_patch",
            {"path": "x.py", "search": "a", "replace": "b"},
            messages=[],
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertIsNotNone(blocked)
        self.assertIn("requires approval", blocked.lower())

    def test_replan_required_blocks_until_replan_update(self):
        middleware = TaskTrackingEnforcementMiddleware()
        state = AgentRuntimeState()
        state.task_board.planning_mode = "light"
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

    def test_progress_is_soft_reminder_not_hard_block(self):
        middleware = TaskTrackingEnforcementMiddleware()
        state = AgentRuntimeState()
        state.task_board.planning_mode = "light"
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
                "ok",
                messages=[],
                runtime_state=state,
                agent_name="main_agent",
            )

        self.assertIsNotNone(reminder)
        self.assertIn("progress", reminder.lower())

    def test_light_mode_requires_final_planning_update_after_action(self):
        middleware = TaskTrackingEnforcementMiddleware()
        state = AgentRuntimeState()
        state.task_board.planning_mode = "light"
        state.task_board.update_count = 1
        state.task_board.needs_final_update = True

        blocked = middleware.pre_exit(messages=[], runtime_state=state, agent_name="main_agent")

        self.assertIsNotNone(blocked)
        self.assertIn("update_plan_state", blocked)
        self.assertIn("final", blocked)


if __name__ == "__main__":
    unittest.main()
