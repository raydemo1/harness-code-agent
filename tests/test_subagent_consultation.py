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
from harness_code_agent.runtime.middlewares import ReadOnlyPlanningMiddleware, ReadOnlySubagentMiddleware


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
        self.assertEqual(result.status, "success")
        self.assertLessEqual(len(result.output), 8200)
        parsed = json.loads(result.output)
        self.assertEqual(parsed["status"], "completed")
        self.assertEqual(parsed["scope"], "codebase_investigation")
        self.assertIn("findings", parsed)

    def test_read_only_subagent_blocks_file_writes(self):
        middleware = ReadOnlySubagentMiddleware()

        blocked = middleware.before_tool(
            "write_file",
            {"path": "x.py", "content": "bad"},
            messages=[],
            agent_name="consult_review",
        )

        self.assertIsNotNone(blocked)
        self.assertIn("read-only", blocked.lower())

    def test_read_only_subagent_blocks_non_read_only_shell_commands(self):
        middleware = ReadOnlySubagentMiddleware()

        dangerous_commands = [
            "rm -rf build",
            "echo hacked > file.txt",
            "printf hacked >> file.txt",
            "sed -i 's/a/b/' file.txt",
            "find . -name '*.pyc' -delete",
        ]

        for command in dangerous_commands:
            with self.subTest(command=command):
                blocked = middleware.before_tool(
                    "run_bash",
                    {"command": command},
                    messages=[],
                    agent_name="consult_review",
                )

                self.assertIsNotNone(blocked)
                self.assertIn("read-only", blocked.lower())

    def test_read_only_subagent_allows_read_only_shell_commands(self):
        middleware = ReadOnlySubagentMiddleware()

        read_only_commands = [
            "git status --short",
            "git diff",
            "rg consult_subagent .",
            "cat tools.py",
        ]

        for command in read_only_commands:
            with self.subTest(command=command):
                blocked = middleware.before_tool(
                    "run_bash",
                    {"command": command},
                    messages=[],
                    agent_name="consult_review",
                )

                self.assertIsNone(blocked)

    def test_plan_tool_schemas_include_constrained_planning_writes(self):
        tool_names = {
            schema["function"]["name"]
            for schema in tools.planning_tool_schemas()
        }

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
                "write_file",
                "apply_patch",
                "update_plan_state",
            },
        )
        self.assertNotIn("browser_test", tool_names)

    def test_planning_profile_allows_plan_artifact_tools(self):
        middleware = ReadOnlyPlanningMiddleware()

        allowed_calls = [
            ("write_file", {"path": "global_plan/current/plan.md", "content": "# Plan\n"}),
            ("write_file", {"path": "PLAN.md", "content": "# Plan\n"}),
            ("write_file", {"path": "plan.markdown", "content": "# Plan\n"}),
            ("apply_patch", {"path": "global_plan/current/plan.markdown", "search": "a", "replace": "b"}),
            ("update_plan_state", {}),
        ]

        for tool_name, tool_args in allowed_calls:
            with self.subTest(tool_name=tool_name, tool_args=tool_args):
                blocked = middleware.before_tool(
                    tool_name,
                    tool_args,
                    messages=[],
                    agent_name="main_agent",
                )

                self.assertIsNone(blocked)

    def test_planning_profile_blocks_non_plan_writes_and_browser_tools(self):
        middleware = ReadOnlyPlanningMiddleware()

        blocked_calls = [
            ("write_file", {"path": "src/app.py", "content": "x"}),
            ("apply_patch", {"path": "README.md", "search": "a", "replace": "b"}),
            ("write_file", {"path": ".harness/sessions/s/planning/state.json", "content": "{}"}),
            ("browser_test", {}),
        ]

        for tool_name, tool_args in blocked_calls:
            with self.subTest(tool_name=tool_name, tool_args=tool_args):
                blocked = middleware.before_tool(
                    tool_name,
                    tool_args,
                    messages=[],
                    agent_name="main_agent",
                )

                self.assertIsNotNone(blocked)
                self.assertIn("planning", blocked.lower())

    def test_read_only_planning_blocks_mutating_shell_commands(self):
        middleware = ReadOnlyPlanningMiddleware()

        dangerous_commands = [
            "rm -rf build",
            "echo hacked > file.txt",
            "printf hacked >> file.txt",
            "git add .",
            "python -m pip install pytest",
        ]

        for command in dangerous_commands:
            with self.subTest(command=command):
                blocked = middleware.before_tool(
                    "run_bash",
                    {"command": command},
                    messages=[],
                    agent_name="main_agent",
                )

                self.assertIsNotNone(blocked)
                self.assertIn("read-only shell", blocked.lower())

    def test_read_only_planning_allows_investigation_and_test_commands(self):
        middleware = ReadOnlyPlanningMiddleware()

        read_only_calls = [
            ("read_file", {"path": "app.py"}),
            ("list_files", {"directory": "."}),
            ("read_skill_file", {"path": "skills/x/SKILL.md"}),
            ("web_search", {"query": "docs"}),
            ("web_fetch", {"url": "https://example.com"}),
            ("consult_subagent", {"task": "inspect", "scope": "review"}),
            ("run_bash", {"command": "rg consult_subagent ."}),
            ("run_bash", {"command": "git diff -- tests"}),
            ("run_bash", {"command": "cat harness_code_agent/runtime/tools.py"}),
            ("run_bash", {"command": "python -m unittest discover -s tests"}),
            ("run_bash", {"command": "pytest tests"}),
        ]

        for tool_name, args in read_only_calls:
            with self.subTest(tool_name=tool_name, args=args):
                blocked = middleware.before_tool(
                    tool_name,
                    args,
                    messages=[],
                    agent_name="main_agent",
                )

                self.assertIsNone(blocked)


if __name__ == "__main__":
    unittest.main()


