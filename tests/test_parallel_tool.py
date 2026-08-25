import os
import shutil
import sys
import threading
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def _install_fake_openai_module() -> None:
    openai = types.ModuleType("openai")

    class OpenAI:
        def __init__(self, *args, **kwargs):
            pass

    openai.OpenAI = OpenAI
    sys.modules["openai"] = openai


_install_fake_openai_module()

from harness_code_agent.runtime import tools
from harness_code_agent.runtime.mcp import McpClientManager, McpToolBinding
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

    def test_planner_allows_independent_files_to_share_a_wave(self):
        root = self.context.workspace.root
        effects = [
            tools.CallEffect((tools.ResourceClaim("workspace", str(root / "a.txt").casefold(), "exact", "write"),)),
            tools.CallEffect((tools.ResourceClaim("workspace", str(root / "b.txt").casefold(), "exact", "write"),)),
        ]
        planner = tools.ExecutionPlanner(enumerate(effects))

        self.assertEqual(planner.ready({0, 1}, set()), [0, 1])

    def test_planner_serializes_same_file_read_write(self):
        key = str(self.context.workspace.root / "sample.txt").casefold()
        planner = tools.ExecutionPlanner([
            (0, tools.CallEffect((tools.ResourceClaim("workspace", key, "exact", "read"),))),
            (1, tools.CallEffect((tools.ResourceClaim("workspace", key, "exact", "write"),))),
        ])

        self.assertEqual(planner.ready({0, 1}, set()), [0])
        self.assertEqual(planner.ready({1}, {0}), [1])

    def test_network_read_can_overlap_local_write(self):
        network = tools.BUILTIN_TOOL_REGISTRY.effect_for("web_search", {"query": "x"}, self.context)
        local_write = tools.BUILTIN_TOOL_REGISTRY.effect_for("write_file", {"path": "a.txt"}, self.context)
        planner = tools.ExecutionPlanner([(0, network), (1, local_write)])

        self.assertEqual(planner.ready({0, 1}, set()), [0, 1])

    def test_shell_inspections_parallel_but_verifications_serialize(self):
        inspections = [
            tools.BUILTIN_TOOL_REGISTRY.effect_for("run_bash", {"command": command}, self.context)
            for command in ("git status --short", "rg needle .")
        ]
        verifications = [
            tools.BUILTIN_TOOL_REGISTRY.effect_for("run_bash", {"command": command}, self.context)
            for command in ("pytest -q", "bun run check")
        ]

        inspect_plan = tools.ExecutionPlanner(enumerate(inspections))
        verify_plan = tools.ExecutionPlanner(enumerate(verifications))
        self.assertEqual(inspect_plan.ready({0, 1}, set()), [0, 1])
        self.assertEqual(verify_plan.ready({0, 1}, set()), [0])

    def test_mcp_read_only_hint_and_server_write_effects(self):
        manager = McpClientManager(workspace=self.root)
        manager.tool_bindings = [
            McpToolBinding("mcp_read_a", "docs", "a", "", {}, "network_read", {"readOnlyHint": True}),
            McpToolBinding("mcp_read_b", "docs", "b", "", {}, "network_read", {"readOnlyHint": True}),
            McpToolBinding("mcp_write_a", "state", "a", "", {}, "dangerous", {}),
            McpToolBinding("mcp_write_b", "state", "b", "", {}, "dangerous", {}),
        ]
        registry = tools.ToolRegistry()
        manager.register_tools(registry)

        read_plan = tools.ExecutionPlanner([
            (0, registry.effect_for("mcp_read_a", {}, self.context)),
            (1, registry.effect_for("mcp_read_b", {}, self.context)),
        ])
        write_plan = tools.ExecutionPlanner([
            (0, registry.effect_for("mcp_write_a", {}, self.context)),
            (1, registry.effect_for("mcp_write_b", {}, self.context)),
        ])

        self.assertEqual(read_plan.ready({0, 1}, set()), [0, 1])
        self.assertEqual(write_plan.ready({0, 1}, set()), [0])

    def test_coordinator_allows_different_file_writes_to_overlap(self):
        coordinator = tools.ResourceCoordinator()
        both_entered = threading.Event()
        release = threading.Event()
        entered = []

        def worker(key):
            claim = tools.ResourceClaim("workspace", key, "exact", "write")
            with coordinator.acquire((claim,)):
                entered.append(key)
                if len(entered) == 2:
                    both_entered.set()
                release.wait(2)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(worker, key) for key in ("a", "b")]
            self.assertTrue(both_entered.wait(1))
            release.set()
            for future in futures:
                future.result()

    def test_coordinator_serializes_subtree_read_and_child_write(self):
        coordinator = tools.ResourceCoordinator()
        reader_entered = threading.Event()
        release_reader = threading.Event()
        writer_entered = threading.Event()
        subtree_key = tools.BUILTIN_TOOL_REGISTRY.effect_for(
            "list_files", {"directory": "src"}, self.context
        ).resources[0].key
        child_key = tools.BUILTIN_TOOL_REGISTRY.effect_for(
            "write_file", {"path": "src/a.py"}, self.context
        ).resources[0].key

        def reader():
            with coordinator.acquire((tools.ResourceClaim("workspace", subtree_key, "subtree", "read"),)):
                reader_entered.set()
                release_reader.wait(2)

        def writer():
            reader_entered.wait(1)
            with coordinator.acquire((tools.ResourceClaim("workspace", child_key, "exact", "write"),)):
                writer_entered.set()

        with ThreadPoolExecutor(max_workers=2) as executor:
            read_future = executor.submit(reader)
            write_future = executor.submit(writer)
            self.assertTrue(reader_entered.wait(1))
            self.assertFalse(writer_entered.wait(0.1))
            release_reader.set()
            self.assertTrue(writer_entered.wait(1))
            read_future.result()
            write_future.result()

    def test_legacy_parallel_tools_are_removed(self):
        schema_names = {schema["function"]["name"] for schema in tools.TOOL_SCHEMAS}

        self.assertNotIn("parallel_commands", schema_names)
        self.assertNotIn("parallel_agents", schema_names)
        self.assertNotIn("parallel", schema_names)
        self.assertIn("spawn_agent", schema_names)


if __name__ == "__main__":
    unittest.main()
