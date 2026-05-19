import json
import tempfile
import unittest
from pathlib import Path


class ProductRuntimeTests(unittest.TestCase):
    def test_builtin_tool_registry_preserves_legacy_schema_and_dispatch_exports(self):
        from harness_code_agent.runtime import tools

        registry_names = {
            schema["function"]["name"]
            for schema in tools.BUILTIN_TOOL_REGISTRY.schemas()
        }
        legacy_names = {
            schema["function"]["name"]
            for schema in tools.TOOL_SCHEMAS + tools.BROWSER_TOOL_SCHEMAS
            if schema["function"]["name"] != "stop_dev_server"
        }

        self.assertEqual(registry_names, legacy_names)
        self.assertIs(tools.BUILTIN_TOOL_REGISTRY.get("read_file"), tools.TOOL_DISPATCH["read_file"])
        self.assertIsNone(tools.BUILTIN_TOOL_REGISTRY.get("missing_tool"))

    def test_session_store_creates_metadata_and_jsonl_events(self):
        from harness_code_agent.sessions.store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / ".harness")
            session = store.create(
                profile="terminal",
                cwd=Path(tmp),
                model="test-model",
                permission_mode="workspace-write",
            )
            bus = store.event_bus(session)
            bus.emit("session_started", agent="main_agent", payload={"task": "fix bug"})

            metadata = json.loads(session.metadata_path.read_text(encoding="utf-8"))
            events = [
                json.loads(line)
                for line in session.events_path.read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual(metadata["profile"], "terminal")
            self.assertEqual(metadata["model"], "test-model")
            self.assertEqual(metadata["permission_mode"], "workspace-write")
            self.assertEqual(events[0]["type"], "session_started")
            self.assertEqual(events[0]["sequence"], 1)

    def test_session_store_lists_and_reads_sessions(self):
        from harness_code_agent.sessions.store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / ".harness")
            first = store.create(
                profile="terminal",
                cwd=Path(tmp),
                model="model-a",
                permission_mode="workspace-write",
            )
            second = store.create(
                profile="plan",
                cwd=Path(tmp),
                model="model-b",
                permission_mode="read-only",
            )
            store.event_bus(second).emit("session_finished", agent="main_agent", payload={})

            sessions = store.list_sessions()
            metadata = store.read_metadata(second.id)
            events = store.read_events(second.id)

            self.assertEqual([item["id"] for item in sessions], [second.id, first.id])
            self.assertEqual(metadata["profile"], "plan")
            self.assertEqual(events[0]["type"], "session_finished")

    def test_session_store_forks_session_metadata_and_lineage_event(self):
        from harness_code_agent.sessions.store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / ".harness")
            source = store.create(
                profile="coding-agent",
                cwd=Path(tmp),
                model="model-a",
                permission_mode="workspace-write",
            )
            store.event_bus(source).emit("session_started", agent="main_agent", payload={})
            store.event_bus(source).emit("session_finished", agent="main_agent", payload={})

            fork = store.fork(source.id)
            metadata = store.read_metadata(fork.id)
            events = store.read_events(fork.id)

            self.assertNotEqual(fork.id, source.id)
            self.assertEqual(metadata["profile"], "coding-agent")
            self.assertEqual(metadata["model"], "model-a")
            self.assertEqual(metadata["permission_mode"], "workspace-write")
            self.assertEqual(metadata["forked_from"], source.id)
            self.assertEqual(metadata["forked_from_event_count"], 2)
            self.assertEqual(events[0]["type"], "session_forked")
            self.assertEqual(events[0]["payload"]["source_session_id"], source.id)

    def test_session_store_reads_fork_lineage_and_resumed_metadata(self):
        from harness_code_agent.sessions.store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / ".harness")
            source = store.create(
                profile="coding-agent",
                cwd=Path(tmp),
                model="model-a",
                permission_mode="workspace-write",
            )
            fork = store.fork(source.id)
            resumed = store.create(
                profile="coding-agent",
                cwd=Path(tmp),
                model="model-a",
                permission_mode="workspace-write",
                resumed_from=fork.id,
            )

            lineage = store.read_lineage(fork.id)
            resumed_metadata = store.read_metadata(resumed.id)

            self.assertEqual([item["id"] for item in lineage], [source.id, fork.id])
            self.assertEqual(resumed_metadata["resumed_from"], fork.id)

    def test_workspace_service_resolves_paths_and_snapshots_before_write(self):
        from harness_code_agent.workspace.service import WorkspaceService

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots")
            target = root / "src" / "app.py"
            target.parent.mkdir()
            target.write_text("old", encoding="utf-8")

            result = workspace.write_text("src/app.py", "new")

            self.assertEqual(target.read_text(encoding="utf-8"), "new")
            self.assertTrue(result.snapshot_path.exists())
            self.assertEqual(result.snapshot_path.read_text(encoding="utf-8"), "old")
            with self.assertRaises(ValueError):
                workspace.resolve("../outside.txt")

    def test_workspace_service_applies_unique_text_patch_and_rejects_ambiguous_patch(self):
        from harness_code_agent.workspace.service import WorkspaceService

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots")
            target = root / "app.py"
            target.write_text("alpha\nbeta\n", encoding="utf-8")

            result = workspace.apply_text_patch("app.py", search="beta\n", replace="gamma\n")

            self.assertEqual(target.read_text(encoding="utf-8"), "alpha\ngamma\n")
            self.assertTrue(result.snapshot_path.exists())

            target.write_text("same\nsame\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                workspace.apply_text_patch("app.py", search="same\n", replace="once\n")
            self.assertEqual(target.read_text(encoding="utf-8"), "same\nsame\n")

    def test_workspace_service_rolls_back_latest_snapshot_for_file(self):
        from harness_code_agent.workspace.service import WorkspaceService

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots")
            target = root / "app.py"
            target.write_text("old\n", encoding="utf-8")
            workspace.write_text("app.py", "new\n")

            result = workspace.rollback_latest_snapshot("app.py")

            self.assertEqual(target.read_text(encoding="utf-8"), "old\n")
            self.assertTrue(result.snapshot_path.exists())

    def test_permission_policy_uses_codex_sandbox_modes(self):
        from harness_code_agent.runtime.permissions import PermissionPolicy

        read_only_policy = PermissionPolicy(mode="read-only")
        read_decision = read_only_policy.decide_tool_call("read_file", {"path": "x.txt"})
        write_decision = read_only_policy.decide_tool_call("write_file", {"path": "x.txt"})
        shell_decision = read_only_policy.decide_tool_call(
            "run_bash",
            {"command": "git status --short"},
        )

        workspace_policy = PermissionPolicy(mode="workspace-write")
        edit_decision = workspace_policy.decide_tool_call("write_file", {"path": "x.txt"})
        safe_shell_decision = workspace_policy.decide_tool_call(
            "run_bash",
            {"command": "git status --short"},
        )
        dangerous_decision = workspace_policy.decide_tool_call(
            "run_bash",
            {"command": "rm -rf build"},
        )

        full_access_policy = PermissionPolicy(mode="danger-full-access")
        full_access_decision = full_access_policy.decide_tool_call(
            "run_bash",
            {"command": "git reset --hard"},
        )

        self.assertTrue(read_decision.allowed)
        self.assertTrue(write_decision.requires_approval)
        self.assertTrue(shell_decision.requires_approval)
        self.assertTrue(edit_decision.allowed)
        self.assertTrue(safe_shell_decision.allowed)
        self.assertTrue(dangerous_decision.requires_approval)
        self.assertEqual(dangerous_decision.risk, "shell_dangerous")
        self.assertTrue(full_access_decision.allowed)

    def test_permission_policy_rejects_unknown_mode_names(self):
        from harness_code_agent.runtime.permissions import PermissionPolicy

        with self.assertRaises(ValueError):
            PermissionPolicy(mode="unsupported-mode")

    def test_execute_tool_with_context_records_events_snapshots_and_approval_denial(self):
        from harness_code_agent.sessions.events import EventBus
        from harness_code_agent.runtime.permissions import PermissionPolicy
        from harness_code_agent.runtime.tool_context import ToolContext
        from harness_code_agent.workspace.service import WorkspaceService
        from harness_code_agent.runtime import tools

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "note.txt").write_text("old", encoding="utf-8")
            events_path = root / ".harness" / "events.jsonl"
            workspace = WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots")
            context = ToolContext(
                workspace=workspace,
                permission_policy=PermissionPolicy(mode="workspace-write"),
                event_bus=EventBus(events_path),
            )

            result = tools.execute_tool(
                "write_file",
                {"path": "note.txt", "content": "new"},
                tool_context=context,
                agent_name="main_agent",
            )
            approval_denied = tools.execute_tool(
                "run_bash",
                {"command": "rm -rf build"},
                tool_context=context,
                agent_name="main_agent",
            )

            events = [
                json.loads(line)
                for line in events_path.read_text(encoding="utf-8").splitlines()
            ]

            self.assertIn("Wrote", result)
            self.assertEqual((root / "note.txt").read_text(encoding="utf-8"), "new")
            self.assertEqual(len(list((root / ".harness" / "snapshots").rglob("*.*"))), 1)
            self.assertIn("[approval_denied]", approval_denied)
            self.assertTrue(events[5]["payload"]["requires_approval"])
            self.assertEqual([event["type"] for event in events], [
                "before_tool",
                "permission_decided",
                "file_changed",
                "after_tool",
                "before_tool",
                "permission_decided",
                "approval_requested",
                "approval_decided",
                "after_tool",
            ])

    def test_execute_tool_apply_patch_records_snapshot_and_rejects_ambiguous_patch(self):
        from harness_code_agent.sessions.events import EventBus
        from harness_code_agent.runtime.permissions import PermissionPolicy
        from harness_code_agent.runtime.tool_context import ToolContext
        from harness_code_agent.workspace.service import WorkspaceService
        from harness_code_agent.runtime import tools

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "note.txt").write_text("old\n", encoding="utf-8")
            events_path = root / ".harness" / "events.jsonl"
            context = ToolContext(
                workspace=WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots"),
                permission_policy=PermissionPolicy(mode="workspace-write"),
                event_bus=EventBus(events_path),
            )

            result = tools.execute_tool(
                "apply_patch",
                {"path": "note.txt", "search": "old\n", "replace": "new\n"},
                tool_context=context,
                agent_name="main_agent",
            )
            ambiguous = tools.execute_tool(
                "apply_patch",
                {"path": "note.txt", "search": "", "replace": "x"},
                tool_context=context,
                agent_name="main_agent",
            )

            events = [
                json.loads(line)
                for line in events_path.read_text(encoding="utf-8").splitlines()
            ]

            self.assertIn("Patched note.txt", result)
            self.assertIn("[error] ValueError", ambiguous)
            self.assertEqual((root / "note.txt").read_text(encoding="utf-8"), "new\n")
            self.assertTrue(any(event["type"] == "file_changed" for event in events))

    def test_execute_tool_runs_approved_tool_call(self):
        from harness_code_agent.runtime.approvals import StaticApprovalProvider
        from harness_code_agent.sessions.events import EventBus
        from harness_code_agent.runtime.permissions import PermissionPolicy
        from harness_code_agent.runtime.tool_context import ToolContext
        from harness_code_agent.workspace.service import WorkspaceService
        from harness_code_agent.runtime import tools

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events_path = root / ".harness" / "events.jsonl"
            context = ToolContext(
                workspace=WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots"),
                permission_policy=PermissionPolicy(mode="read-only"),
                event_bus=EventBus(events_path),
                approval_provider=StaticApprovalProvider(approved=True, reason="test approval"),
            )

            result = tools.execute_tool(
                "write_file",
                {"path": "approved.txt", "content": "ok"},
                tool_context=context,
                agent_name="main_agent",
            )
            events = [
                json.loads(line)
                for line in events_path.read_text(encoding="utf-8").splitlines()
            ]

            self.assertIn("Wrote", result)
            self.assertEqual((root / "approved.txt").read_text(encoding="utf-8"), "ok")
            self.assertEqual(events[2]["type"], "approval_requested")
            self.assertEqual(events[3]["type"], "approval_decided")
            self.assertTrue(events[3]["payload"]["approved"])


if __name__ == "__main__":
    unittest.main()


