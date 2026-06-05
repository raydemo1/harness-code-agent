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
from harness_code_agent.runtime import tools
from harness_code_agent.runtime.middlewares import (
    PreExitVerificationMiddleware,
    RecoveryStrategyMiddleware,
    TaskTrackingEnforcementMiddleware,
)
from harness_code_agent.runtime.tool_result import ToolResult


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

    def test_all_profiles_expose_tool_search(self):
        for profile_meta in list_profiles():
            with self.subTest(profile=profile_meta["name"]):
                main_agent = get_profile(profile_meta["name"]).main_agent()
                tool_names = {
                    schema["function"]["name"]
                    for schema in tools.tool_schemas_for_profile(
                        allowed_permissions=main_agent.allowed_tool_permissions,
                        include_names=main_agent.allowed_tool_names,
                        exclude_names=main_agent.blocked_tool_names,
                    )
                }

                self.assertIn("tool_search", tool_names)

    def test_terminal_main_agent_prompt_keeps_single_owner_model(self):
        profile = get_profile("terminal")
        prompt = profile.main_agent().system_prompt.lower()

        self.assertIn("you are the main agent", prompt)
        self.assertIn("only you may modify files", prompt)
        self.assertIn("consult_subagent", prompt)
        self.assertNotIn("planner", prompt)
        self.assertNotIn("builder", prompt)
        self.assertNotIn("evaluator", prompt)

    def test_plan_profile_allows_only_plan_state_writes_and_is_structured(self):
        main_agent = get_profile("plan").main_agent()
        prompt = main_agent.system_prompt.lower()
        tool_names = {
            schema["function"]["name"]
            for schema in tools.tool_schemas_for_profile(
                allowed_permissions=main_agent.allowed_tool_permissions,
                include_names=main_agent.allowed_tool_names,
                exclude_names=main_agent.blocked_tool_names,
            )
        }

        self.assertIn("planning task", prompt)
        self.assertIn("decision-complete", prompt)
        self.assertIn("# title", prompt)
        self.assertIn("## summary", prompt)
        self.assertIn("## implementation changes", prompt)
        self.assertIn("## test plan", prompt)
        self.assertIn("## assumptions", prompt)
        self.assertIn("do not implement", prompt)
        self.assertIn("planning state", prompt)
        self.assertIn("update_plan_state", prompt)
        self.assertEqual(
            tool_names,
            {
                "read_file",
                "list_files",
                "read_skill_file",
                "memory_search",
                "read_memory_file",
                "tool_search",
                "web_search",
                "web_fetch",
                "ask_user",
                "consult_subagent",
                "update_plan_state",
            },
        )
        self.assertNotIn("run_bash", tool_names)
        self.assertFalse(any(name.startswith("browser") for name in tool_names))

    def test_review_profile_is_registered_and_read_only(self):
        from harness_code_agent.profiles.review import ReviewOnlyMiddleware

        profile_names = [profile["name"] for profile in list_profiles()]
        main_agent = get_profile("review").main_agent()
        prompt = main_agent.system_prompt.lower()
        tool_names = {
            schema["function"]["name"]
            for schema in tools.tool_schemas_for_profile(
                allowed_permissions=main_agent.allowed_tool_permissions,
                include_names=main_agent.allowed_tool_names,
                exclude_names=main_agent.blocked_tool_names,
            )
        }

        self.assertIn("review", profile_names)
        self.assertIn("findings first", prompt)
        self.assertIn("read-only", prompt)
        self.assertIn("must not modify files", prompt)
        self.assertIn("do not implement", prompt)
        self.assertIn("cannot manage or stop shell jobs", prompt)
        self.assertEqual(
            tool_names,
            {
                "read_file",
                "list_files",
                "read_skill_file",
                "memory_search",
                "read_memory_file",
                "tool_search",
                "web_search",
                "web_fetch",
                "consult_subagent",
                "list_shell_jobs",
                "read_shell_output",
                "run_bash",
            },
        )
        self.assertTrue(any(isinstance(mw, ReviewOnlyMiddleware) for mw in main_agent.middlewares))

    def test_review_only_middleware_blocks_writes_and_risky_shell(self):
        from harness_code_agent.profiles.review import ReviewOnlyMiddleware

        middleware = ReviewOnlyMiddleware()

        write_block = middleware.before_tool("write_file", {"path": "x.py", "content": "bad"}, [])
        shell_block = middleware.before_tool("run_bash", {"command": "git add ."}, [])
        verify_allowed = middleware.before_tool("run_bash", {"command": "pytest tests"}, [])
        pipeline_allowed = middleware.before_tool("run_bash", {"command": "rg foo . | head -n 5"}, [])
        redirect_block = middleware.before_tool("run_bash", {"command": "rg foo . > out.txt"}, [])
        tee_block = middleware.before_tool("run_bash", {"command": "rg foo . | tee out.txt"}, [])
        stop_job_block = middleware.before_tool("stop_shell_job", {"job_id": "shell-job-1"}, [])
        read_allowed = middleware.before_tool("read_file", {"path": "x.py"}, [])

        self.assertIsInstance(write_block, ToolResult)
        self.assertIsInstance(shell_block, ToolResult)
        self.assertIsInstance(redirect_block, ToolResult)
        self.assertIsInstance(tee_block, ToolResult)
        self.assertIsInstance(stop_job_block, ToolResult)
        self.assertIn("read-only", write_block.output)
        self.assertIn("safe verification", shell_block.output)
        self.assertIn("safe verification", redirect_block.output)
        self.assertIn("safe verification", tee_block.output)
        self.assertIsNone(verify_allowed)
        self.assertIsNone(pipeline_allowed)
        self.assertIsNone(read_allowed)

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
        self.assertIn("inspect git diff", swe_prompt)

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

    def test_runtime_profiles_require_verification_not_forced_final_review(self):
        for profile_name in ["coding-agent", "app-builder", "swe-bench", "terminal"]:
            with self.subTest(profile=profile_name):
                main_agent = get_profile(profile_name).main_agent()
                prompt = main_agent.system_prompt.lower()
                middleware_prompts = " ".join(
                    getattr(mw, "_verification_prompt", "")
                    for mw in main_agent.middlewares
                ).lower()

                self.assertIn("verification", prompt + " " + middleware_prompts)
                self.assertNotIn("final review", prompt + " " + middleware_prompts)

    def test_runtime_profiles_expose_shell_job_tools(self):
        for profile_name in ["coding-agent", "app-builder", "swe-bench", "terminal"]:
            with self.subTest(profile=profile_name):
                main_agent = get_profile(profile_name).main_agent()
                prompt = main_agent.system_prompt.lower()
                tool_names = {
                    schema["function"]["name"]
                    for schema in tools.tool_schemas_for_profile(
                        allowed_permissions=main_agent.allowed_tool_permissions,
                        include_names=main_agent.allowed_tool_names,
                        exclude_names=main_agent.blocked_tool_names,
                    )
                }

                self.assertIn("list_shell_jobs", tool_names)
                self.assertIn("read_shell_output", tool_names)
                self.assertIn("stop_shell_job", tool_names)
                self.assertIn("read_shell_output", prompt)


if __name__ == "__main__":
    unittest.main()
