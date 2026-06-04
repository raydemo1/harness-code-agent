import json
import sys
import types
import unittest
from unittest.mock import patch


def _install_fake_openai_module() -> None:
    openai = types.ModuleType("openai")

    class OpenAI:
        def __init__(self, *args, **kwargs):
            pass

    openai.OpenAI = OpenAI
    sys.modules["openai"] = openai


_install_fake_openai_module()

from harness_code_agent.runtime import tools


class CapturingAgent:
    init_kwargs = None

    def __init__(self, **kwargs):
        self.__class__.init_kwargs = kwargs

    def run(self, task):
        return "x" * 9000


class SubagentConsultationTests(unittest.TestCase):
    def test_consult_subagent_uses_read_only_tool_schemas(self):
        with patch("harness_code_agent.agent.loop.Agent", CapturingAgent):
            result = tools.consult_subagent("inspect auth flow", scope="codebase_investigation")

        tool_names = [
            schema["function"]["name"]
            for schema in CapturingAgent.init_kwargs["tool_schemas"]
        ]
        self.assertIn("read_file", tool_names)
        self.assertIn("list_files", tool_names)
        self.assertIn("run_bash", tool_names)
        self.assertIn("web_search", tool_names)
        self.assertNotIn("write_file", tool_names)
        self.assertNotIn("update_plan_state", tool_names)
        self.assertTrue(CapturingAgent.init_kwargs["middlewares"])
        self.assertEqual(result.status, "success")
        self.assertLessEqual(len(result.output), 8200)
        parsed = json.loads(result.output)
        self.assertEqual(parsed["status"], "completed")
        self.assertEqual(parsed["scope"], "codebase_investigation")
        self.assertIn("findings", parsed)

    def test_consultation_tool_schema_helper_is_stably_ordered(self):
        self.assertEqual(
            [schema["function"]["name"] for schema in tools.consultation_tool_schemas()],
            ["read_file", "list_files", "run_bash", "web_search", "web_fetch"],
        )

    def test_consultation_middleware_allows_read_and_verify_shell_only(self):
        middleware = tools.ConsultationReadOnlyMiddleware()

        diff_allowed = middleware.before_tool("run_bash", {"command": "git diff --stat"}, [])
        verify_allowed = middleware.before_tool("run_bash", {"command": "pytest tests"}, [])
        pipeline_allowed = middleware.before_tool("run_bash", {"command": "rg foo . | head -n 5"}, [])
        write_block = middleware.before_tool("write_file", {"path": "x.py", "content": "bad"}, [])
        shell_block = middleware.before_tool("run_bash", {"command": "git add ."}, [])
        redirect_block = middleware.before_tool("run_bash", {"command": "rg foo . > out.txt"}, [])

        self.assertIsNone(diff_allowed)
        self.assertIsNone(verify_allowed)
        self.assertIsNone(pipeline_allowed)
        self.assertIsNotNone(write_block)
        self.assertIsNotNone(shell_block)
        self.assertIsNotNone(redirect_block)
        self.assertIn("read-only", write_block.output)
        self.assertIn("read-only or verification", shell_block.output)
        self.assertIn("read-only or verification", redirect_block.output)


if __name__ == "__main__":
    unittest.main()

