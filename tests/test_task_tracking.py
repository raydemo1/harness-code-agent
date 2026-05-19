import os
import shutil
import sys
import types
import unittest
from pathlib import Path


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


class UpdatePlanningFilesToolTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = os.path.join(os.getcwd(), "workspace", "test-planning-tool")
        self.old_workspace = config.WORKSPACE
        os.makedirs(self.temp_dir, exist_ok=True)
        config.WORKSPACE = self.temp_dir

    def tearDown(self):
        config.WORKSPACE = self.old_workspace
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_light_mode_updates_runtime_state_and_progress_file(self):
        state = AgentRuntimeState()
        result = execute_tool(
            "update_planning_files",
            {
                "mode": "light",
                "goal": "fix task",
                "steps": ["inspect", "edit", "verify"],
                "current_step": "inspect",
                "completed_steps": [],
                "blockers": [],
                "next_action": "read tests",
            },
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertIn("Updated planning files", result)
        self.assertEqual(state.task_board.planning_mode, "light")
        self.assertEqual(state.task_board.current_step, "inspect")
        self.assertEqual(state.task_board.update_count, 1)
        self.assertTrue(Path(self.temp_dir, config.PROGRESS_FILE).exists())
        self.assertFalse(Path(self.temp_dir, "task_plan.md").exists())
        self.assertFalse(Path(self.temp_dir, "findings.md").exists())

    def test_full_mode_writes_all_planning_files(self):
        state = AgentRuntimeState()
        result = execute_tool(
            "update_planning_files",
            {
                "mode": "full",
                "goal": "build feature",
                "steps": ["plan", "implement", "verify"],
                "current_step": "plan",
                "completed_steps": [],
                "blockers": [],
                "next_action": "inspect repo",
                "task_plan": "# Task Plan\n\n- plan\n",
                "findings": "# Findings\n\n- none yet\n",
            },
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertIn("task_plan.md", result)
        self.assertTrue(Path(self.temp_dir, config.PROGRESS_FILE).exists())
        self.assertTrue(Path(self.temp_dir, "task_plan.md").exists())
        self.assertTrue(Path(self.temp_dir, "findings.md").exists())

    def test_skip_mode_sets_state_without_files(self):
        state = AgentRuntimeState()
        result = execute_tool(
            "update_planning_files",
            {
                "mode": "skip",
                "goal": "quick check",
                "steps": ["run command"],
                "current_step": "run command",
                "completed_steps": [],
                "blockers": [],
                "next_action": "run pwd",
            },
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertIn("skip", result)
        self.assertEqual(state.task_board.planning_mode, "skip")
        self.assertFalse(Path(self.temp_dir, config.PROGRESS_FILE).exists())


class TaskTrackingEnforcementTests(unittest.TestCase):
    def test_main_agent_cannot_run_bash_before_planning_mode_self_check(self):
        middleware = TaskTrackingEnforcementMiddleware()
        state = AgentRuntimeState()

        blocked = middleware.before_tool(
            "run_bash",
            {"command": "pwd"},
            messages=[],
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertIsNotNone(blocked)
        self.assertIn("update_planning_files", blocked)

    def test_skip_mode_allows_action_without_planning_files(self):
        middleware = TaskTrackingEnforcementMiddleware()
        state = AgentRuntimeState()
        state.task_board.planning_mode = "skip"
        state.task_board.update_count = 1

        blocked = middleware.before_tool(
            "run_bash",
            {"command": "pwd"},
            messages=[],
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertIsNone(blocked)

    def test_light_mode_requires_final_planning_update_after_action(self):
        middleware = TaskTrackingEnforcementMiddleware()
        state = AgentRuntimeState()
        state.task_board.planning_mode = "light"
        state.task_board.update_count = 1
        state.task_board.needs_final_update = True

        blocked = middleware.pre_exit(messages=[], runtime_state=state, agent_name="main_agent")

        self.assertIsNotNone(blocked)
        self.assertIn("update_planning_files", blocked)


if __name__ == "__main__":
    unittest.main()


