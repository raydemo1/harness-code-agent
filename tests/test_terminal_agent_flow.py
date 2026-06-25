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
from harness_code_agent.runtime.middlewares import (
    AcceptanceReviewMiddleware,
    PreExitVerificationMiddleware,
    RecoveryStrategyMiddleware,
    TaskTrackingEnforcementMiddleware,
    TerminalShellEditPolicyMiddleware,
)
from harness_code_agent.profiles.terminal import TerminalProfile


class AgentRuntimeStateTests(unittest.TestCase):
    def test_agent_creates_runtime_state_once_per_run(self):
        agent = Agent(name="main_agent", system_prompt="x", use_tools=False)
        state = agent._create_runtime_state("goal text")

        self.assertEqual(state.task_board.goal, "goal text")
        self.assertEqual(state.task_board.planning_mode, "unset")
        self.assertEqual(state.recovery.mode, "NORMAL")
        self.assertIsNone(state.shell_session)

    def test_agent_can_start_in_light_planning_mode(self):
        agent = Agent(
            name="main_agent",
            system_prompt="x",
            use_tools=False,
            initial_planning_mode="light",
        )
        conversation = agent.start_conversation("goal text")
        try:
            self.assertEqual(conversation.runtime_state.task_board.goal, "goal text")
            self.assertEqual(conversation.runtime_state.task_board.planning_mode, "light")

            conversation.add_user_turn("next goal")

            self.assertEqual(conversation.runtime_state.task_board.goal, "next goal")
            self.assertEqual(conversation.runtime_state.task_board.planning_mode, "light")
        finally:
            conversation.close()

    def test_terminal_main_agent_prompt_mentions_stateful_shell_progress_and_recovery(self):
        cfg = TerminalProfile().main_agent()
        prompt = cfg.system_prompt

        self.assertIn("persistent shell", prompt.lower())
        self.assertIn("update_plan_state", prompt)
        self.assertIn("## Planning Mode", prompt)
        self.assertIn("PROBE", prompt)
        self.assertIn("light planning mode", prompt)
        self.assertIn("acceptance_checks", prompt)
        self.assertIn("exact output", prompt)
        self.assertIn("switch strategy", prompt)
        self.assertIn("background services", prompt)
        self.assertIn("shell-driven batch workflow", prompt)
        self.assertIn("repository-wide search", prompt)
        self.assertIn("same-pattern mechanical edits", prompt)
        self.assertIn("dry-running the intended file set", prompt)
        self.assertEqual(cfg.initial_planning_mode, "light")

    def test_terminal_main_agent_uses_enforcement_middlewares(self):
        middlewares = TerminalProfile().main_agent().middlewares

        self.assertTrue(any(isinstance(mw, TaskTrackingEnforcementMiddleware) for mw in middlewares))
        self.assertTrue(any(isinstance(mw, RecoveryStrategyMiddleware) for mw in middlewares))
        self.assertTrue(any(isinstance(mw, AcceptanceReviewMiddleware) for mw in middlewares))
        self.assertTrue(any(isinstance(mw, TerminalShellEditPolicyMiddleware) for mw in middlewares))
        self.assertFalse(any(isinstance(mw, PreExitVerificationMiddleware) for mw in middlewares))

    def test_terminal_light_mode_blocks_first_action_until_plan_start(self):
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

        self.assertIsNotNone(blocked)
        self.assertIn("update_plan_state", blocked)
        self.assertIn("start", blocked)


if __name__ == "__main__":
    unittest.main()


