import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from harness_code_agent.profiles import router


class HybridProfileRouterTests(unittest.TestCase):
    def test_low_confidence_timeout_and_invalid_json_stay_in_current_profile(self):
        low = router.route_profile_for_turn(
            "继续看看",
            current_profile="review",
            confidence_threshold=1.1,
            llm_classifier=lambda **_: router.LlmRouteResult(
                profile_name="coding-agent",
                confidence=0.79,
            ),
        )
        timed_out = router.route_profile_for_turn(
            "继续看看",
            current_profile="review",
            confidence_threshold=1.1,
            llm_classifier=lambda **_: (_ for _ in ()).throw(TimeoutError("slow")),
        )
        invalid = router._parse_llm_route_result("not json")
        non_finite = router._parse_llm_route_result(
            '{"profile": "review", "confidence": NaN, "reason": "bad"}'
        )

        self.assertEqual(low.profile_name, "review")
        self.assertEqual(low.failure_type, "low_confidence")
        self.assertTrue(low.fallback_used)
        self.assertEqual(timed_out.profile_name, "review")
        self.assertEqual(timed_out.failure_type, "timeout")
        self.assertEqual(invalid.failure_type, "invalid_json")
        self.assertEqual(non_finite.failure_type, "invalid_confidence")

    def test_implementation_question_does_not_enter_review_without_explicit_review_request(self):
        decision = router.route_profile_for_turn(
            "这个实现并发时安全吗？只给结论",
            current_profile="coding-agent",
            llm_classifier=lambda **_: router.LlmRouteResult(
                profile_name="coding-agent",
                confidence=0.60,
                reason="Question about the work already in progress.",
            ),
        )

        self.assertEqual(decision.profile_name, "coding-agent")
        self.assertEqual(decision.action, router.ROUTE_ACTION_STAY)
        self.assertTrue(decision.fallback_used)
        self.assertEqual(decision.failure_type, "low_confidence")

    def test_fast_classifier_uses_bounded_client_and_context(self):
        calls = {}

        class FakeClient:
            def with_options(self, **kwargs):
                calls["options"] = kwargs
                return self

            @property
            def chat(self):
                return SimpleNamespace(completions=self)

            def create(self, **kwargs):
                calls["request"] = kwargs
                content = json.dumps({
                    "profile": "review",
                    "confidence": 0.91,
                    "reason": "Explicit code review requested.",
                })
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
                )

        with patch("harness_code_agent.agent.providers.get_client", return_value=FakeClient()):
            result = router._classify_with_fast_llm(
                user_prompt="审查这个补丁",
                current_profile="coding-agent",
                previous_user_task="fix parser",
                previous_assistant_text="parser updated",
            )

        self.assertEqual(calls["options"], {"timeout": 3.0, "max_retries": 0})
        payload = json.loads(calls["request"]["messages"][1]["content"])
        self.assertEqual(payload["previous_user_task"], "fix parser")
        self.assertEqual(payload["previous_assistant_answer"], "parser updated")
        self.assertEqual(result.profile_name, "review")
        self.assertEqual(result.confidence, 0.91)


if __name__ == "__main__":
    unittest.main()
