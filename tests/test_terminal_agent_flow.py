import sys
import types
import unittest


def _install_fake_openai_module() -> None:
    openai = types.ModuleType("openai")

    class OpenAI:
        def __init__(self, *args, **kwargs):
            pass

    openai.OpenAI = OpenAI
    sys.modules["openai"] = openai


_install_fake_openai_module()

from harness_code_agent.agent.loop import Agent
from harness_code_agent.runtime.middlewares import TaskTrackingEnforcementMiddleware
from harness_code_agent.profiles.terminal import TerminalProfile


class AgentRuntimeStateTests(unittest.TestCase):
    def test_agent_creates_runtime_state_once_per_run(self):
        agent = Agent(name="main_agent", system_prompt="x", use_tools=False)
        state = agent._create_runtime_state("goal text")

        self.assertEqual(state.task_board.goal, "goal text")
        self.assertEqual(state.task_board.planning_mode, "unset")
        self.assertEqual(state.recovery.mode, "NORMAL")
        self.assertIsNone(state.shell_session)

    def test_agent_can_start_in_tracked_mode(self):
        agent = Agent(
            name="main_agent",
            system_prompt="x",
            use_tools=False,
            initial_planning_mode="tracked",
        )
        conversation = agent.start_conversation("goal text")
        try:
            self.assertEqual(conversation.runtime_state.task_board.goal, "goal text")
            self.assertEqual(conversation.runtime_state.task_board.planning_mode, "tracked")

            conversation.add_user_turn("next goal")

            self.assertEqual(conversation.runtime_state.task_board.goal, "next goal")
            self.assertEqual(conversation.runtime_state.task_board.planning_mode, "tracked")
        finally:
            conversation.close()

    def test_terminal_main_agent_prompt_mentions_stateful_shell_progress_and_recovery(self):
        cfg = TerminalProfile().main_agent()
        prompt = cfg.system_prompt

        self.assertIn("persistent shell", prompt.lower())
        self.assertIn("update_plan_state", prompt)
        self.assertIn("tracked mode", prompt)
        self.assertIn("acceptance_checks", prompt)
        self.assertIn("exact output", prompt)
        self.assertIn("switch strategy", prompt)
        self.assertIn("background services", prompt)
        self.assertIn("verification-first", prompt)
        self.assertIn("Spec:", prompt)
        self.assertIn("Risks:", prompt)
        self.assertIn("Validation:", prompt)
        self.assertIn("Implement:", prompt)
        self.assertIn("hidden-verifier checks", prompt)
        self.assertIn("local substitute", prompt)
        self.assertIn("On every replan", prompt)
        self.assertIn("shell-driven batch workflow", prompt)
        self.assertIn("repository-wide search", prompt)
        self.assertIn("Shell-driven file writes are allowed", prompt)
        self.assertIn("inside the task workspace", prompt)
        self.assertIn("preview or explain broad edits", prompt)
        self.assertEqual(cfg.initial_planning_mode, "tracked")

    def test_terminal_tracked_mode_allows_first_action_and_reminds_about_plan_start(self):
        cfg = TerminalProfile().main_agent()
        state = Agent(
            name="main_agent",
            system_prompt=cfg.system_prompt,
            use_tools=False,
            initial_planning_mode=cfg.initial_planning_mode,
        )._create_runtime_state("terminal task")
        middleware = next(mw for mw in cfg.middlewares if isinstance(mw, TaskTrackingEnforcementMiddleware))

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
                    "ok",
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

        self.assertIsNone(blocked)
        self.assertTrue(all(post is None for post in early_post))
        self.assertIn("update_plan_state", reminder)
        self.assertIn("start", reminder)


if __name__ == "__main__":
    unittest.main()


