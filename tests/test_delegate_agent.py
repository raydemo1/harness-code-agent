import json
import os
import shutil
import sys
import types
import unittest
from pathlib import Path
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
from harness_code_agent.runtime.permissions import PermissionPolicy
from harness_code_agent.runtime.tool_context import ToolContext
from harness_code_agent.sessions.events import EventBus
from harness_code_agent.workspace.service import WorkspaceService


class CapturingAgent:
    init_kwargs = None

    def __init__(self, **kwargs):
        self.__class__.init_kwargs = kwargs
        self.tool_context = kwargs.get("tool_context")

    def run(self, task):
        if self.tool_context and "delegate_patch" in self.__class__.init_kwargs["name"]:
            self.tool_context.workspace.write_text("app.py", "print('patched')\n")
        return json.dumps({
            "status": "completed",
            "agent": self.__class__.init_kwargs["name"].replace("delegate_", ""),
            "mode": "read_only",
            "summary": "done",
            "findings": ["finding"],
            "evidence": ["app.py:1"],
            "recommendations": [],
            "risks": [],
            "verification": [],
        })


class DelegatePathAllowlistTests(unittest.TestCase):
    """Lexical path checks for delegated tool calls."""

    def test_dot_dot_escape_outside_allowed_root_is_rejected(self):
        from harness_code_agent.agent.delegation import _path_allowed

        self.assertFalse(_path_allowed("src/../../etc/passwd", ["src"]))
        self.assertFalse(_path_allowed("../../workspace/secret", ["docs"]))
        self.assertFalse(_path_allowed("a/../..", ["a"]))

    def test_normal_relative_paths_inside_allowed_root_are_accepted(self):
        from harness_code_agent.agent.delegation import _path_allowed

        self.assertTrue(_path_allowed("src/app.py", ["src"]))
        self.assertTrue(_path_allowed("src/a/../app.py", ["src"]))
        self.assertTrue(_path_allowed(".\\src\\app.py", ["src"]))
        self.assertTrue(_path_allowed("./src", ["src"]))
        self.assertTrue(_path_allowed("docs", ["docs"]))

    def test_sibling_prefix_directory_is_not_confused_for_allowed_root(self):
        from harness_code_agent.agent.delegation import _path_allowed

        self.assertFalse(_path_allowed("docs2/x.md", ["docs"]))
        self.assertFalse(_path_allowed("src_private/app.py", ["src"]))


class DelegateAgentTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(os.getcwd(), "workspace", "test-delegate-agent")
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True)
        (self.root / "app.py").write_text("print('original')\n", encoding="utf-8")
        self.context = ToolContext(
            workspace=WorkspaceService(root=self.root),
            permission_policy=PermissionPolicy(mode="danger-full-access"),
            event_bus=EventBus(),
        )

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_delegate_agent_uses_profile_specific_tool_schemas(self):
        with patch("harness_code_agent.agent.conversation.Agent", CapturingAgent):
            result = tools.delegate_agent(
                agent_profile="explore",
                task="inspect auth flow",
                tool_context=self.context,
            )

        tool_names = [
            schema["function"]["name"]
            for schema in CapturingAgent.init_kwargs["tool_schemas"]
        ]
        self.assertEqual(result.status, "success")
        self.assertIn("read_file", tool_names)
        self.assertIn("run_bash", tool_names)
        self.assertNotIn("write_file", tool_names)
        parsed = json.loads(result.output)
        self.assertEqual(parsed["agent"], "explore")

    def test_patch_delegate_returns_diff_without_mutating_real_workspace(self):
        with patch("harness_code_agent.agent.conversation.Agent", CapturingAgent):
            result = tools.delegate_agent(
                agent_profile="patch",
                task="draft a patch",
                tool_context=self.context,
            )

        self.assertEqual(result.status, "success")
        self.assertEqual((self.root / "app.py").read_text(encoding="utf-8"), "print('original')\n")
        parsed = json.loads(result.output)
        self.assertIn("app.py", parsed["changed_files"])
        self.assertIn("print('patched')", parsed["proposed_patch"])

    def test_parallel_agents_rejects_patch_profile(self):
        result = tools.parallel_agents(
            [{"id": "patch", "agent_profile": "patch", "task": "draft"}],
            tool_context=self.context,
        )

        self.assertEqual(result.status, "failed")
        self.assertIn("only allows explore", result.output)

    def test_parallel_commands_rejects_mutating_command(self):
        result = tools.parallel_commands(
            [{"id": "write", "command": "git add ."}],
            tool_context=self.context,
        )

        self.assertEqual(result.status, "failed")
        self.assertIn("only read-only or verification", result.output)

    def test_new_tools_are_registered_and_old_tools_are_not(self):
        schema_names = {schema["function"]["name"] for schema in tools.TOOL_SCHEMAS}

        self.assertIn("delegate_agent", schema_names)
        self.assertIn("parallel_agents", schema_names)
        self.assertIn("parallel_commands", schema_names)
        self.assertNotIn("consult_subagent", schema_names)
        self.assertNotIn("parallel", schema_names)
        self.assertEqual(tools.BUILTIN_TOOL_REGISTRY.permission_for("delegate_agent"), "read")


if __name__ == "__main__":
    unittest.main()
