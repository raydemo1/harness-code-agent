import time
import unittest

from harness_code_agent.runtime.middleware.time_budget import TimeBudgetMiddleware


class TimeBudgetMiddlewareTests(unittest.TestCase):
    def test_warning_threshold_returns_guidance_without_runtime_exception(self):
        middleware = TimeBudgetMiddleware(
            budget_seconds=100,
            warn_threshold=0.5,
            critical_threshold=0.9,
        )
        middleware.start_time = time.time() - 60

        guidance = middleware.per_iteration(1, [])

        self.assertIsNotNone(guidance)
        self.assertIn("Time check", guidance)


if __name__ == "__main__":
    unittest.main()
