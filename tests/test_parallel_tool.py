import tempfile
import time
import unittest
from pathlib import Path

from harness_code_agent.runtime import tools
from harness_code_agent.runtime.permissions import PermissionPolicy
from harness_code_agent.runtime.tool_context import ToolContext
from harness_code_agent.runtime.tool_result import ToolResult
from harness_code_agent.sessions.events import EventBus
from harness_code_agent.workspace.service import WorkspaceService


class ParallelToolTests(unittest.TestCase):
    def _context(self, root: Path, registry=None) -> ToolContext:
        return ToolContext(
            workspace=WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots"),
            permission_policy=PermissionPolicy(mode="danger-full-access"),
            event_bus=EventBus(),
            tool_registry=registry or tools.BUILTIN_TOOL_REGISTRY,
            allowed_tool_permissions={"read", "network_read", "edit", "control", "shell"},
            blocked_tool_names=set(),
            revealed_tool_names=set(),
        )

    def test_parallel_runs_independent_read_tools_concurrently_and_keeps_order(self):
        registry = tools.BUILTIN_TOOL_REGISTRY.copy()

        def slow_read(label, delay=0.2):
            time.sleep(delay)
            return ToolResult(tool="slow_read", status="success", output=f"done {label}")

        registry.register(
            {
                "type": "function",
                "function": {
                    "name": "slow_read",
                    "description": "slow read",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            slow_read,
            permission="read",
            lane=tools.ToolExecutionLane.WORKSPACE_READ,
        )

        with tempfile.TemporaryDirectory() as tmp:
            context = self._context(Path(tmp), registry=registry)
            start = time.perf_counter()
            result = tools.parallel(
                tool_uses=[
                    {"id": "a", "tool_name": "slow_read", "arguments": {"label": "a", "delay": 0.25}},
                    {"id": "b", "tool_name": "slow_read", "arguments": {"label": "b", "delay": 0.25}},
                ],
                tool_context=context,
            )
            elapsed = time.perf_counter() - start

        self.assertLess(elapsed, 0.45)
        self.assertEqual(result.status, "success")
        self.assertLess(result.output.index("[1] a"), result.output.index("[2] b"))
        self.assertIn("done a", result.output)
        self.assertIn("done b", result.output)

    def test_parallel_combines_builtin_repository_inspection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("alpha\nbeta\n", encoding="utf-8")
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text("print('alpha')\n", encoding="utf-8")
            context = self._context(root)

            result = tools.parallel(
                tool_uses=[
                    {"id": "tree", "tool_name": "list_files", "arguments": {"directory": ".", "depth": 2}},
                    {"id": "readme", "tool_name": "read_file", "arguments": {"path": "README.md", "max_lines": 2}},
                    {"id": "search", "tool_name": "repo_search", "arguments": {"pattern": "alpha", "path": "."}},
                ],
                tool_context=context,
            )

        self.assertEqual(result.status, "success")
        self.assertIn("[1] tree kind=list_files status=success", result.output)
        self.assertIn("README.md", result.output)
        self.assertIn("[2] readme kind=read_file status=success", result.output)
        self.assertEqual(result.metadata["tool_use_count"], 3)
        self.assertEqual(result.metadata["success_count"], 3)

    def test_parallel_rejects_edit_shell_control_and_recursive_tools(self):
        result = tools.parallel(
            tool_uses=[
                {"id": "write", "tool_name": "write_file", "arguments": {"path": "x.txt", "content": "nope"}},
                {"id": "shell", "tool_name": "run_bash", "arguments": {"command": "git status --short"}},
                {"id": "plan", "tool_name": "update_plan_state", "arguments": {}},
                {"id": "recursive", "tool_name": "parallel", "arguments": {"tool_uses": []}},
            ],
        )

        self.assertEqual(result.status, "failed")
        self.assertIn("write_file", result.output)
        self.assertIn("run_bash", result.output)
        self.assertIn("update_plan_state", result.output)
        self.assertIn("parallel cannot call itself", result.output)
        self.assertEqual(result.metadata["failed_count"], 4)

    def test_parallel_event_output_is_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("secret-ish source text\n", encoding="utf-8")
            context = self._context(root)

            result = tools.execute_tool_result(
                "parallel",
                {"tool_uses": [{"tool_name": "read_file", "arguments": {"path": "README.md"}}]},
                tool_context=context,
            )

        self.assertEqual(result.status, "success")
        tool_results = [event for event in context.event_bus.events if event.type == "tool_result"]
        self.assertEqual(len(tool_results), 1)
        self.assertIn("[redacted parallel output:", tool_results[0].payload["output"])
        self.assertTrue(tool_results[0].payload["metadata"]["output_redacted"])

    def test_parallel_call_event_redacts_nested_content_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = self._context(root)
            large_content = "x" * 5000

            tools.execute_tool_result(
                "parallel",
                {
                    "tool_uses": [
                        {
                            "tool_name": "write_file",
                            "arguments": {"path": "x.txt", "content": large_content},
                        }
                    ]
                },
                tool_context=context,
            )

        tool_calls = [event for event in context.event_bus.events if event.type == "tool_call"]
        args = tool_calls[0].payload["args"]
        nested_content = args["tool_uses"][0]["arguments"]["content"]
        self.assertEqual(nested_content, "[5000 chars]")
        self.assertNotIn(large_content, str(args))

    def test_parallel_is_registered_as_read_only_parallel_lane(self):
        schema_names = {schema["function"]["name"] for schema in tools.TOOL_SCHEMAS}

        self.assertIn("parallel", schema_names)
        self.assertNotIn("batch_read", schema_names)
        self.assertEqual(tools.BUILTIN_TOOL_REGISTRY.permission_for("parallel"), "read")
        self.assertEqual(
            tools.BUILTIN_TOOL_REGISTRY.lane_for("parallel"),
            tools.ToolExecutionLane.WORKSPACE_READ,
        )


if __name__ == "__main__":
    unittest.main()
