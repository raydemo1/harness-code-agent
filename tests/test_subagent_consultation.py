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

import tools
from middlewares import ReadOnlySubagentMiddleware


class CapturingAgent:
    init_kwargs = None

    def __init__(self, **kwargs):
        self.__class__.init_kwargs = kwargs

    def run(self, task):
        return "x" * 9000


class SubagentConsultationTests(unittest.TestCase):
    def test_consult_subagent_uses_read_only_tool_schemas(self):
        with patch("agents.Agent", CapturingAgent):
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
        self.assertNotIn("update_progress", tool_names)
        self.assertLessEqual(len(result), 8200)
        parsed = json.loads(result)
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

    def test_delegate_task_is_not_available_as_a_tool(self):
        tool_names = {schema["function"]["name"] for schema in tools.TOOL_SCHEMAS}

        self.assertNotIn("delegate_task", tool_names)
        self.assertNotIn("delegate_task", tools.TOOL_DISPATCH)
        self.assertIn("Unknown tool", tools.execute_tool("delegate_task", {"task": "do work"}))


if __name__ == "__main__":
    unittest.main()
