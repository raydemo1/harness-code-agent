from __future__ import annotations

import unittest
from types import SimpleNamespace

from harness_code_agent.agent.turn_controller import TurnController


class _Trace:
    def __init__(self) -> None:
        self.finished: list[tuple[str, int]] = []
        self.injections: list[tuple[str, str, str]] = []
        self.errors: list[tuple[str, str]] = []

    def finish(self, reason: str, iteration: int) -> None:
        self.finished.append((reason, iteration))

    def middleware_inject(self, name: str, phase: str, content: str) -> None:
        self.injections.append((name, phase, content))

    def error(self, kind: str, message: str) -> None:
        self.errors.append((kind, message))


class _Middleware:
    def __init__(self, injection: str | None) -> None:
        self.injection = injection

    def pre_exit(self, messages, runtime_state=None, agent_name=None):
        return self.injection


def _conversation(*, middleware=None, queued=False):
    messages: list[dict] = []
    trace = _Trace()
    conversation = SimpleNamespace(
        agent=SimpleNamespace(name="agent", middlewares=[middleware] if middleware else [], time_budget=None),
        runtime_state=SimpleNamespace(),
        messages=messages,
        trace=trace,
        _drain_queued_messages=lambda: queued,
        _append_message=messages.append,
    )
    return conversation


class TurnControllerTests(unittest.TestCase):
    def test_queued_message_wins_before_pre_exit_gate(self):
        middleware = _Middleware("verify first")
        conversation = _conversation(middleware=middleware, queued=True)

        decision = TurnController(conversation).after_no_tool_calls(iteration=2)

        self.assertTrue(decision.continue_loop)
        self.assertEqual(decision.reason, "queued_message")
        self.assertEqual(conversation.messages, [])

    def test_pre_exit_injection_continues_and_records_the_gate(self):
        middleware = _Middleware("verify first")
        conversation = _conversation(middleware=middleware)

        decision = TurnController(conversation).after_no_tool_calls(iteration=2)

        self.assertTrue(decision.continue_loop)
        self.assertEqual(conversation.messages[-1]["content"], "verify first")
        self.assertEqual(conversation.trace.injections[0][1], "pre_exit")

    def test_no_tool_calls_without_gate_stops_once(self):
        conversation = _conversation()

        decision = TurnController(conversation).after_no_tool_calls(iteration=3)

        self.assertFalse(decision.continue_loop)
        self.assertEqual(conversation.trace.finished, [("no_tool_calls", 3)])

    def test_truncated_tool_response_continues_without_repeating_executed_tools(self):
        conversation = _conversation()

        decision = TurnController(conversation).after_tool_calls(
            finish_reason="length",
            iteration=4,
        )

        self.assertTrue(decision.continue_loop)
        self.assertIn("WERE executed successfully", conversation.messages[-1]["content"])
        self.assertIn("Do NOT re-run", conversation.messages[-1]["content"])
        self.assertEqual(conversation.trace.errors, [("length_truncated", "max_tokens hit")])


if __name__ == "__main__":
    unittest.main()
