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

from profiles import get_profile, list_profiles
from profiles.base import AgentConfig, BaseProfile
from middlewares import (
    PreExitVerificationMiddleware,
    RecoveryStrategyMiddleware,
    TaskTrackingEnforcementMiddleware,
)


class ProfileInterfaceTests(unittest.TestCase):
    def test_coding_agent_profile_is_registered_as_product_profile(self):
        profile_names = [profile["name"] for profile in list_profiles()]
        profile = get_profile("coding-agent")

        self.assertIn("coding-agent", profile_names)
        self.assertIn("local repository", profile.description())
        self.assertIn("durable Harness session", profile.main_agent().system_prompt)
        self.assertIn("workspace path checks", profile.main_agent().system_prompt)

    def test_all_profiles_expose_main_agent_and_subagent_policy(self):
        for profile_meta in list_profiles():
            profile = get_profile(profile_meta["name"])

            main_agent = profile.main_agent()
            policy = profile.subagent_policy()

            self.assertIsInstance(main_agent, AgentConfig)
            self.assertIn("consult_subagent", main_agent.system_prompt)
            self.assertIn("allowed_scopes", policy)
            self.assertIn("review", policy["allowed_scopes"])

    def test_terminal_main_agent_prompt_keeps_single_owner_model(self):
        profile = get_profile("terminal")
        prompt = profile.main_agent().system_prompt.lower()

        self.assertIn("you are the main agent", prompt)
        self.assertIn("only you may modify files", prompt)
        self.assertIn("consult_subagent", prompt)
        self.assertNotIn("planner", prompt)
        self.assertNotIn("builder", prompt)
        self.assertNotIn("evaluator", prompt)

    def test_profile_can_exist_without_legacy_role_methods(self):
        class MinimalProfile(BaseProfile):
            def name(self) -> str:
                return "minimal"

            def description(self) -> str:
                return "Minimal main-agent-only profile"

        profile = MinimalProfile()

        self.assertIsInstance(profile.main_agent(), AgentConfig)
        self.assertFalse(profile.planner().enabled)
        self.assertFalse(profile.builder().enabled)
        self.assertFalse(profile.evaluator().enabled)

    def test_builtin_profiles_do_not_prompt_for_delegate_task(self):
        for profile_meta in list_profiles():
            profile = get_profile(profile_meta["name"])

            self.assertNotIn("delegate_task", profile.main_agent().system_prompt)

    def test_app_builder_and_swe_bench_use_core_runtime_guardrails(self):
        for profile_name in ["app-builder", "swe-bench"]:
            with self.subTest(profile=profile_name):
                middlewares = get_profile(profile_name).main_agent().middlewares

                self.assertTrue(any(isinstance(mw, TaskTrackingEnforcementMiddleware) for mw in middlewares))
                self.assertTrue(any(isinstance(mw, RecoveryStrategyMiddleware) for mw in middlewares))
                self.assertTrue(any(isinstance(mw, PreExitVerificationMiddleware) for mw in middlewares))


if __name__ == "__main__":
    unittest.main()
