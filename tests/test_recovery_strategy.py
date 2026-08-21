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

from harness_code_agent.agent.conversation import AgentRuntimeState
from harness_code_agent.runtime.middlewares import LoopDetectionMiddleware, RecoveryStrategyMiddleware


class RecoveryStrategyTests(unittest.TestCase):
    def test_repeated_environment_failures_switch_to_env_fix(self):
        state = AgentRuntimeState()
        middleware = RecoveryStrategyMiddleware()

        middleware.observe_tool_result("run_bash", {"command": "foo"}, "[error] command not found", state)
        middleware.observe_tool_result("run_bash", {"command": "foo"}, "[error] command not found", state)

        self.assertEqual(state.recovery.mode, "ENV_FIX")

    def test_repeated_same_failure_switches_to_spec_recheck(self):
        state = AgentRuntimeState()
        middleware = RecoveryStrategyMiddleware()

        middleware.observe_verification_failure("pytest::task_x failed", state)
        middleware.observe_verification_failure("pytest::task_x failed", state)

        self.assertEqual(state.recovery.mode, "SPEC_RECHECK")
        self.assertTrue(state.task_board.replan_required)
        self.assertIn("pytest::task_x failed", state.task_board.replan_reason)

    def test_repeated_edits_with_same_failure_switch_to_rethink(self):
        state = AgentRuntimeState()
        middleware = RecoveryStrategyMiddleware()

        middleware.observe_verification_failure("assert result.txt mismatch", state)
        middleware.observe_verification_failure("assert result.txt mismatch", state)
        middleware.observe_edit_attempt("result.txt", state)
        middleware.observe_edit_attempt("result.txt", state)

        self.assertEqual(state.recovery.mode, "RETHINK")
        self.assertTrue(state.task_board.requires_update)

    def test_probe_mode_allows_read_only_verification_and_direct_fix(self):
        state = AgentRuntimeState()
        state.recovery.mode = "PROBE"
        middleware = RecoveryStrategyMiddleware()

        edit_block = middleware.before_tool(
            "write_file",
            {"path": "result.txt", "content": "retry"},
            [],
            runtime_state=state,
            agent_name="main_agent",
        )
        state.recovery.mode = "PROBE"
        first_probe = middleware.before_tool(
            "run_bash",
            {"command": "whoami && git --version && which python3"},
            [],
            runtime_state=state,
            agent_name="main_agent",
        )
        middleware.on_tool_allowed(
            "run_bash",
            {"command": "whoami && git --version && which python3"},
            [],
            runtime_state=state,
            agent_name="main_agent",
        )
        second_probe = middleware.before_tool(
            "run_bash",
            {"command": "ls /git 2>/dev/null || echo no-git"},
            [],
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertIsNone(edit_block)
        self.assertIsNone(first_probe)
        self.assertIn("probe", second_probe.lower())

    def test_probe_mode_allows_mutating_compound_commands_to_resume_work(self):
        state = AgentRuntimeState()
        state.recovery.mode = "PROBE"
        state.task_board.requires_update = True
        middleware = RecoveryStrategyMiddleware()

        blocked = middleware.before_tool(
            "run_bash",
            {"command": "mkdir -p /git && git init --bare /git/server"},
            [],
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertIsNone(blocked)
        self.assertEqual(state.recovery.mode, "NORMAL")
        self.assertFalse(state.task_board.requires_update)

    def test_successful_probe_returns_recovery_to_normal(self):
        state = AgentRuntimeState()
        state.recovery.mode = "PROBE"
        state.recovery.probe_in_flight = True
        middleware = RecoveryStrategyMiddleware()

        guidance = middleware.post_tool(
            "run_bash",
            {"command": "pytest -q"},
            "1 passed",
            [],
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertEqual(state.recovery.mode, "NORMAL")
        self.assertFalse(state.recovery.probe_in_flight)
        self.assertFalse(state.task_board.replan_required)
        self.assertIn("probe passed", guidance.lower())

    def test_failed_probe_requires_another_replan(self):
        state = AgentRuntimeState()
        state.recovery.mode = "PROBE"
        state.recovery.probe_in_flight = True
        middleware = RecoveryStrategyMiddleware()

        guidance = middleware.post_tool(
            "run_bash",
            {"command": "pytest -q"},
            "[error] pytest::task_x failed",
            [],
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertEqual(state.recovery.mode, "SPEC_RECHECK")
        self.assertTrue(state.task_board.replan_required)
        self.assertIn("probe failed", guidance.lower())

    def test_file_read_output_does_not_trigger_verification_failure(self):
        """cat/sed reading source code should not be matched as verification failure."""
        state = AgentRuntimeState()
        middleware = RecoveryStrategyMiddleware()

        # Simulate reading source code that contains "failed" and "error" in string literals
        source_output = (
            '1\t#include <stdio.h>\n'
            '2\tvoid handle() {\n'
            '3\t    fprintf(stderr, "Error opening\\n");\n'
            '4\t    // Failed to allocate memory\n'
            '5\t    assert(value > 0);\n'
            '6\t}'
        )

        # cat -n is classified as "read", not "verify" — should NOT trigger
        middleware.post_tool(
            "run_bash",
            {"command": "cat -n sim.c"},
            source_output,
            [],
            runtime_state=state,
            agent_name="main_agent",
        )
        middleware.post_tool(
            "run_bash",
            {"command": "sed -n '1,100p' sim.c"},
            source_output,
            [],
            runtime_state=state,
            agent_name="main_agent",
        )

        # Should NOT have entered SPEC_RECHECK
        self.assertEqual(state.recovery.mode, "NORMAL")
        self.assertFalse(state.task_board.replan_required)

    def test_verify_command_failure_still_triggers_recovery(self):
        """pytest/make test failures should still trigger verification failure detection."""
        state = AgentRuntimeState()
        middleware = RecoveryStrategyMiddleware()

        # pytest is classified as "verify" — SHOULD trigger
        middleware.post_tool(
            "run_bash",
            {"command": "pytest -q"},
            "FAILED test_task.py::test_output - AssertionError: expected 377 got 104",
            [],
            runtime_state=state,
            agent_name="main_agent",
        )
        middleware.post_tool(
            "run_bash",
            {"command": "pytest -q"},
            "FAILED test_task.py::test_output - AssertionError: expected 377 got 104",
            [],
            runtime_state=state,
            agent_name="main_agent",
        )

        # Should have entered SPEC_RECHECK after 2 repeated failures
        self.assertEqual(state.recovery.mode, "SPEC_RECHECK")
        self.assertTrue(state.task_board.replan_required)


class LoopDetectionMiddlewareTests(unittest.TestCase):
    def test_repeated_tool_fingerprint_warns_once_then_requests_stop(self):
        state = AgentRuntimeState()
        middleware = LoopDetectionMiddleware(tool_fingerprint_repeat_threshold=2)

        first = middleware.post_tool(
            "web_search",
            {"query": "agent fallback", "filters": {"b": 2, "a": 1}},
            "ok",
            [],
            runtime_state=state,
            agent_name="main_agent",
        )
        warning = middleware.post_tool(
            "web_search",
            {"filters": {"a": 1, "b": 2}, "query": "agent fallback"},
            "ok",
            [],
            runtime_state=state,
            agent_name="main_agent",
        )
        stop = middleware.post_tool(
            "web_search",
            {"query": "agent fallback", "filters": {"b": 2, "a": 1}},
            "ok",
            [],
            runtime_state=state,
            agent_name="main_agent",
        )

        self.assertIsNone(first)
        self.assertIn("same tool call", warning)
        self.assertIsNone(stop)
        self.assertTrue(state.fallback.stop_requested)
        self.assertEqual(state.fallback.stop_reason, "loop_detected")
        self.assertEqual(state.fallback.stop_last_tool, "web_search")
        self.assertTrue(state.fallback.stop_fingerprint_hash)

    def test_repeated_tool_fingerprint_summary_does_not_leak_large_arguments(self):
        state = AgentRuntimeState()
        middleware = LoopDetectionMiddleware(tool_fingerprint_repeat_threshold=2)
        secret = "SECRET_PAYLOAD_" * 200
        args = {"path": "note.txt", "content": secret}

        middleware.post_tool("custom_tool", args, "ok", [], runtime_state=state, agent_name="main_agent")
        middleware.post_tool("custom_tool", args, "ok", [], runtime_state=state, agent_name="main_agent")
        middleware.post_tool("custom_tool", args, "ok", [], runtime_state=state, agent_name="main_agent")

        self.assertTrue(state.fallback.stop_requested)
        summary = "\n".join(state.fallback.recent_action_summary)
        self.assertIn("custom_tool", summary)
        self.assertNotIn("SECRET_PAYLOAD", summary)
        self.assertLess(len(summary), 500)


if __name__ == "__main__":
    unittest.main()


