import unittest
from types import SimpleNamespace
from unittest.mock import patch


class PreExitVerificationMiddlewareTests(unittest.TestCase):
    def test_allows_plain_identity_question_without_tool_work(self):
        from harness_code_agent.runtime.middleware import (
            ExitIntentDecision,
            PreExitVerificationMiddleware,
        )

        middleware = PreExitVerificationMiddleware()
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "你是谁"},
            {"role": "assistant", "content": "我是 Harness Agent。"},
        ]
        runtime_state = SimpleNamespace(current_turn_start_index=1)

        with patch(
            "harness_code_agent.runtime.middleware.verification.classify_exit_intent",
            return_value=ExitIntentDecision(mode="exit", confidence=0.95, reason="text-only question"),
        ) as classify:
            self.assertIsNone(middleware.pre_exit(messages, runtime_state=runtime_state))

        classify.assert_called_once()

    def test_still_requires_tools_for_actionable_coding_task(self):
        from harness_code_agent.runtime.middleware import (
            ExitIntentDecision,
            PreExitVerificationMiddleware,
        )

        middleware = PreExitVerificationMiddleware()
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "请修复这个 bug"},
            {"role": "assistant", "content": "我会修复。"},
        ]
        runtime_state = SimpleNamespace(current_turn_start_index=1)

        with patch(
            "harness_code_agent.runtime.middleware.verification.classify_exit_intent",
            return_value=ExitIntentDecision(mode="continue", confidence=0.9, reason="needs workspace action"),
        ):
            result = middleware.pre_exit(messages, runtime_state=runtime_state)

        self.assertIsNotNone(result)
        self.assertIn("smallest relevant tool action", result)
        self.assertNotIn("write_file", result)

    def test_low_confidence_continue_is_allowed_to_exit(self):
        from harness_code_agent.runtime.middleware import (
            ExitIntentDecision,
            PreExitVerificationMiddleware,
        )

        middleware = PreExitVerificationMiddleware()
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "帮我看下这个项目是不是健康"},
            {"role": "assistant", "content": "看起来可以先从测试和 README 开始。"},
        ]
        runtime_state = SimpleNamespace(current_turn_start_index=1)

        with patch(
            "harness_code_agent.runtime.middleware.verification.classify_exit_intent",
            return_value=ExitIntentDecision(mode="continue", confidence=0.6, reason="ambiguous"),
        ):
            self.assertIsNone(middleware.pre_exit(messages, runtime_state=runtime_state))

    def test_classifier_failure_defaults_to_exit(self):
        from harness_code_agent.runtime.middleware.verification import (
            classify_exit_intent,
        )

        with patch("harness_code_agent.agent.providers.get_client", side_effect=RuntimeError("offline")):
            decision = classify_exit_intent(user_task="你是谁")

        self.assertEqual(decision.mode, "exit")


if __name__ == "__main__":
    unittest.main()
