import os
import shutil
import sys
import time
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


class ParallelToolTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(os.getcwd(), "workspace", "test-parallel-tools")
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True)
        (self.root / "sample.txt").write_text("needle\n", encoding="utf-8")
        self.context = ToolContext(
            workspace=WorkspaceService(root=self.root),
            permission_policy=PermissionPolicy(mode="danger-full-access"),
            event_bus=EventBus(),
        )

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_parallel_commands_runs_independent_verify_commands(self):
        result = tools.parallel_commands(
            [
                {"id": "python", "command": "python --version", "timeout": 30},
                {"id": "git", "command": "git status --short", "timeout": 30},
            ],
            tool_context=self.context,
        )

        self.assertEqual(result.status, "success")
        self.assertIn("python", result.output.lower())
        self.assertEqual(result.metadata["item_count"], 2)

    def test_parallel_commands_rejects_writes(self):
        result = tools.parallel_commands(
            [{"id": "write", "command": "Set-Content out.txt bad"}],
            tool_context=self.context,
        )

        self.assertEqual(result.status, "failed")
        self.assertIn("only read-only or verification", result.output)

    def test_parallel_agents_runs_read_only_delegates(self):
        calls = []

        def fake_delegate_agent(**kwargs):
            calls.append(kwargs["agent_profile"])
            time.sleep(0.01)
            return tools.ToolResult(
                tool="delegate_agent",
                status="success",
                output=f"ok {kwargs['agent_profile']}",
                metadata={"agent_profile": kwargs["agent_profile"]},
            )

        with patch("harness_code_agent.runtime.builtins.parallel.delegate_agent", side_effect=fake_delegate_agent):
            result = tools.parallel_agents(
                [
                    {"id": "explore", "agent_profile": "explore", "task": "inspect"},
                    {"id": "review", "agent_profile": "review", "task": "review"},
                ],
                tool_context=self.context,
            )

        self.assertEqual(result.status, "success")
        self.assertEqual(calls, ["explore", "review"])
        self.assertEqual(result.metadata["success_count"], 2)

    def test_parallel_agents_rejects_patch(self):
        result = tools.parallel_agents(
            [{"id": "patch", "agent_profile": "patch", "task": "draft"}],
            tool_context=self.context,
        )

        self.assertEqual(result.status, "failed")
        self.assertIn("parallel_agents only allows", result.output)

    def test_parallel_tools_are_registered_separately(self):
        schema_names = {schema["function"]["name"] for schema in tools.TOOL_SCHEMAS}

        self.assertIn("parallel_commands", schema_names)
        self.assertIn("parallel_agents", schema_names)
        self.assertNotIn("parallel", schema_names)
        self.assertEqual(tools.BUILTIN_TOOL_REGISTRY.permission_for("parallel_commands"), "read")
        self.assertEqual(tools.BUILTIN_TOOL_REGISTRY.lane_for("parallel_agents"), tools.ToolExecutionLane.SUBAGENT_READ)


if __name__ == "__main__":
    unittest.main()
