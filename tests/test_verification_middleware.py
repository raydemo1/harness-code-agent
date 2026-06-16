import unittest
from types import SimpleNamespace


class PreExitVerificationMiddlewareTests(unittest.TestCase):
    def test_allows_plain_identity_question_without_tool_work(self):
        from harness_code_agent.runtime.middleware import PreExitVerificationMiddleware

        middleware = PreExitVerificationMiddleware()
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "你是谁"},
            {"role": "assistant", "content": "我是 Harness Agent。"},
        ]
        runtime_state = SimpleNamespace(current_turn_start_index=1)

        self.assertIsNone(middleware.pre_exit(messages, runtime_state=runtime_state))

    def test_still_requires_tools_for_actionable_coding_task(self):
        from harness_code_agent.runtime.middleware import PreExitVerificationMiddleware

        middleware = PreExitVerificationMiddleware()
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "请修复这个 bug"},
            {"role": "assistant", "content": "我会修复。"},
        ]
        runtime_state = SimpleNamespace(current_turn_start_index=1)

        result = middleware.pre_exit(messages, runtime_state=runtime_state)

        self.assertIsNotNone(result)
        self.assertIn("MUST use run_bash", result)


if __name__ == "__main__":
    unittest.main()
