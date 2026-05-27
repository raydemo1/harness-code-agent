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

from harness_code_agent.profiles import get_profile, list_profiles
from harness_code_agent.profiles.base import AgentConfig
from harness_code_agent.planning_policy import PLANNING_MODE_POLICY
from harness_code_agent.runtime.middlewares import (
    PreExitVerificationMiddleware,
    RecoveryStrategyMiddleware,
    ReadOnlyPlanningMiddleware,
    TaskTrackingEnforcementMiddleware,
)


class ProfileInterfaceTests(unittest.TestCase):
    def test_coding_agent_profile_is_registered_as_product_profile(self):
        profile_names = [profile["name"] for profile in list_profiles()]
        profile = get_profile("coding-agent")

        self.assertIn("coding-agent", profile_names)
        self.assertIn("plan", profile_names)
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

    def test_plan_profile_is_read_only_and_structured(self):
        main_agent = get_profile("plan").main_agent()
        prompt = main_agent.system_prompt.lower()
        tool_names = {
            schema["function"]["name"]
            for schema in main_agent.tool_schemas
        }

        self.assertIn("read-only planning task", prompt)
        self.assertIn("decision-complete", prompt)
        self.assertIn("# title", prompt)
        self.assertIn("## summary", prompt)
        self.assertIn("## implementation changes", prompt)
        self.assertIn("## test plan", prompt)
        self.assertIn("## assumptions", prompt)
        self.assertIn("do not implement", prompt)
        self.assertIn("do not call update_plan_state", prompt)
        self.assertIn("do not create any planning artifact", prompt)
        self.assertEqual(
            tool_names,
            {
                "read_file",
                "list_files",
                "read_skill_file",
                "run_bash",
                "web_search",
                "web_fetch",
                "consult_subagent",
            },
        )
        self.assertFalse({"write_file", "apply_patch", "update_plan_state"} & tool_names)
        self.assertFalse(any(name.startswith("browser") for name in tool_names))
        self.assertTrue(any(isinstance(mw, ReadOnlyPlanningMiddleware) for mw in main_agent.middlewares))

    def test_specialized_profile_prompts_capture_expected_workflows(self):
        app_prompt = get_profile("app-builder").main_agent().system_prompt.lower()
        swe_prompt = get_profile("swe-bench").main_agent().system_prompt.lower()

        self.assertIn("real source files", app_prompt)
        self.assertIn("browser_test", app_prompt)
        self.assertIn("console errors", app_prompt)
        self.assertIn("responsive behavior", app_prompt)
        self.assertIn("accessibility", app_prompt)
        self.assertIn("reproduce or characterize the failure", swe_prompt)
        self.assertIn("smallest diff", swe_prompt)
        self.assertIn("regression tests", swe_prompt)
        self.assertIn("review git diff", swe_prompt)

    def test_app_builder_and_swe_bench_use_core_runtime_guardrails(self):
        for profile_name in ["app-builder", "swe-bench"]:
            with self.subTest(profile=profile_name):
                middlewares = get_profile(profile_name).main_agent().middlewares

                self.assertTrue(any(isinstance(mw, TaskTrackingEnforcementMiddleware) for mw in middlewares))
                self.assertTrue(any(isinstance(mw, RecoveryStrategyMiddleware) for mw in middlewares))
                self.assertTrue(any(isinstance(mw, PreExitVerificationMiddleware) for mw in middlewares))

    def test_runtime_profiles_use_shared_planning_mode_policy(self):
        for profile_name in ["coding-agent", "app-builder", "swe-bench", "terminal"]:
            with self.subTest(profile=profile_name):
                prompt = get_profile(profile_name).main_agent().system_prompt

                self.assertIn(PLANNING_MODE_POLICY, prompt)


if __name__ == "__main__":
    unittest.main()


