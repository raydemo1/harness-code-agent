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
        }

        self.assertEqual(registry_names, legacy_names)
        self.assertIs(tools.BUILTIN_TOOL_REGISTRY.get("read_file"), tools.TOOL_DISPATCH["read_file"])
        self.assertIs(tools.BUILTIN_TOOL_REGISTRY.get("stop_dev_server"), tools.TOOL_DISPATCH["stop_dev_server"])
        self.assertIsNone(tools.BUILTIN_TOOL_REGISTRY.get("missing_tool"))

    def test_structured_event_schema_covers_mvp_event_types(self):
        from harness_code_agent.sessions.events import (
            AssistantMessageEvent,
            FailureEvent,
            FileChangeEvent,
            SessionFinishedEvent,
            TaskOutcomeEvent,
            ToolCallEvent,
            ToolResultEvent,
            UserInputEvent,
        )

        event_types = {
            UserInputEvent(text="fix").to_event().type,
            AssistantMessageEvent(text="done").to_event().type,
            ToolCallEvent(tool="read_file", args={"path": "README.md"}).to_event().type,
            ToolResultEvent(tool="read_file", status="success", output="ok").to_event().type,
            FileChangeEvent(path="app.py").to_event().type,
            FailureEvent(category="tool_error", message="boom").to_event().type,
            SessionFinishedEvent(reason="user_exit", status="closed").to_event().type,
            TaskOutcomeEvent(status="success", evidence=["tests_passed"], summary="done").to_event().type,
        }

        self.assertEqual(event_types, {
            "user_input",
            "assistant_message",
            "tool_call",
            "tool_result",
            "file_change",
            "failure",
            "session_finished",
            "task_outcome",
        })

    def test_tool_result_serializes_and_tool_execution_records_structured_events(self):
        from harness_code_agent.runtime.permissions import PermissionPolicy
        from harness_code_agent.runtime.tool_context import ToolContext
        from harness_code_agent.runtime.tool_result import ToolResult
        from harness_code_agent.sessions.events import EventBus
        from harness_code_agent.workspace.service import WorkspaceService
        from harness_code_agent.runtime import tools

        result = ToolResult(
            tool="read_file",
            status="failed",
            output="",
            error="missing",
            return_code=2,
            metadata={"path": "missing.txt"},
        )

        self.assertEqual(result.to_dict()["tool"], "read_file")
        self.assertEqual(result.to_dict()["status"], "failed")
        self.assertFalse(result.to_dict()["ok"])
        self.assertEqual(result.to_dict()["error"], "missing")
        self.assertEqual(result.to_text(), "[error] missing")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "note.txt").write_text("hello", encoding="utf-8")
            events_path = root / ".harness" / "events.jsonl"
            context = ToolContext(
                workspace=WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots"),
                permission_policy=PermissionPolicy(mode="workspace-write"),
                event_bus=EventBus(events_path),
            )

            output = tools.execute_tool(
                "read_file",
                {"path": "note.txt"},
                tool_context=context,
                agent_name="main_agent",
            )
            events = [
                json.loads(line)
                for line in events_path.read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual(output, "hello")
            self.assertIn("tool_call", [event["type"] for event in events])
            self.assertIn("tool_result", [event["type"] for event in events])
            tool_result = next(event for event in events if event["type"] == "tool_result")
            self.assertEqual(tool_result["payload"]["tool"], "read_file")
            self.assertEqual(tool_result["payload"]["status"], "success")
            self.assertTrue(tool_result["payload"]["ok"])
            self.assertEqual(tool_result["payload"]["output"], "hello")

    def test_tool_result_does_not_infer_status_from_raw_tool_text(self):
        from unittest.mock import patch

        from harness_code_agent.runtime.permissions import PermissionPolicy
        from harness_code_agent.runtime.tool_context import ToolContext
        from harness_code_agent.sessions.events import EventBus
        from harness_code_agent.workspace.service import WorkspaceService
        from harness_code_agent.runtime import tools

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events_path = root / ".harness" / "events.jsonl"
            context = ToolContext(
                workspace=WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots"),
                permission_policy=PermissionPolicy(mode="workspace-write"),
                event_bus=EventBus(events_path),
            )

            with patch.object(
                tools.BUILTIN_TOOL_REGISTRY,
                "get",
                return_value=lambda **kwargs: "[error] this is domain output, not execution status",
            ):
                output = tools.execute_tool(
                    "custom_tool",
                    {},
                    tool_context=context,
                    agent_name="main_agent",
                )

            events = [
                json.loads(line)
                for line in events_path.read_text(encoding="utf-8").splitlines()
            ]
            tool_result = [event for event in events if event["type"] == "tool_result"][0]

            self.assertEqual(output, "[error] this is domain output, not execution status")
            self.assertEqual(tool_result["payload"]["status"], "unknown")
            self.assertIsNone(tool_result["payload"]["ok"])
            self.assertEqual(tool_result["payload"]["metadata"]["status_source"], "unstructured")
            self.assertFalse(any(event["type"] == "failure" for event in events))

    def test_unknown_tool_records_structured_failure_events(self):
        from harness_code_agent.runtime.permissions import PermissionPolicy
        from harness_code_agent.runtime.tool_context import ToolContext
        from harness_code_agent.sessions.events import EventBus
        from harness_code_agent.workspace.service import WorkspaceService
        from harness_code_agent.runtime import tools

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events_path = root / ".harness" / "events.jsonl"
            context = ToolContext(
                workspace=WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots"),
                permission_policy=PermissionPolicy(mode="workspace-write"),
                event_bus=EventBus(events_path),
            )

            output = tools.execute_tool(
                "missing_tool",
                {"secret": "nope"},
                tool_context=context,
                agent_name="main_agent",
            )

            events = [
                json.loads(line)
                for line in events_path.read_text(encoding="utf-8").splitlines()
            ]
            event_types = [event["type"] for event in events]
            tool_result = next(event for event in events if event["type"] == "tool_result")
            after_tool = next(event for event in events if event["type"] == "after_tool")

            self.assertEqual(output, "[error] Unknown tool: missing_tool")
            self.assertIn("tool_call", event_types)
            self.assertIn("failure", event_types)
            self.assertEqual(tool_result["payload"]["status"], "failed")
            self.assertFalse(tool_result["payload"]["ok"])
            self.assertEqual(after_tool["payload"]["status"], "failed")

    def test_tool_validation_failures_return_typed_failed_results(self):
        from harness_code_agent.runtime.permissions import PermissionPolicy
        from harness_code_agent.runtime.tool_context import ToolContext
        from harness_code_agent.sessions.events import EventBus
        from harness_code_agent.workspace.service import WorkspaceService
        from harness_code_agent.runtime import tools

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events_path = root / ".harness" / "events.jsonl"
            context = ToolContext(
                workspace=WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots"),
                permission_policy=PermissionPolicy(mode="workspace-write"),
                event_bus=EventBus(events_path),
            )

            missing = tools.execute_tool(
                "read_file",
                {"path": "missing.txt"},
                tool_context=context,
                agent_name="main_agent",
            )
            empty_write = tools.execute_tool(
                "write_file",
                {"path": "", "content": "x"},
                tool_context=context,
                agent_name="main_agent",
            )
            empty_patch = tools.execute_tool(
                "apply_patch",
                {"path": "", "search": "x", "replace": "y"},
                tool_context=context,
                agent_name="main_agent",
            )

            events = [
                json.loads(line)
                for line in events_path.read_text(encoding="utf-8").splitlines()
            ]
            failed_results = [
                event for event in events
                if event["type"] == "tool_result"
                and event["payload"].get("status") == "failed"
            ]

            self.assertIn("[error] File not found: missing.txt", missing)
            self.assertIn("[auto-fix] Empty file path", empty_write)
            self.assertIn("[error] Empty file path", empty_patch)
            self.assertEqual(len(failed_results), 3)
            self.assertEqual(len([event for event in events if event["type"] == "failure"]), 3)

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

    def test_session_store_reads_latest_session_and_persisted_summary(self):
        from harness_code_agent.sessions.store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / ".harness")
            first = store.create(
                profile="coding-agent",
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
            store.event_bus(second).emit("user_input", agent="main_agent", payload={"text": "plan it"})
            summary = store.write_summary(second.id)

            latest = store.latest_session()

            self.assertEqual(latest["id"], second.id)
            self.assertIn("Session summary", summary)
            self.assertIn("profile: plan", store.read_summary(second.id))
            with self.assertRaises(FileNotFoundError):
                store.read_summary(first.id)

    def test_session_summary_formats_human_readable_event_overview(self):
        from harness_code_agent.sessions.summary import format_session_summary

        metadata = {
            "id": "session-a",
            "profile": "coding-agent",
            "model": "model-a",
            "permission_mode": "workspace-write",
            "status": "running",
            "cwd": "C:/workspace",
            "created_at": "2026-05-20T00:00:00+00:00",
            "forked_from": "session-parent",
        }
        events = [
            {"sequence": 1, "type": "session_started", "agent": "main_agent", "payload": {}},
            {"sequence": 2, "type": "turn_started", "agent": "main_agent", "payload": {"turn": 1}},
            {"sequence": 3, "type": "after_tool", "agent": "main_agent", "payload": {"tool": "write_file", "ok": True}},
            {"sequence": 4, "type": "file_changed", "agent": "main_agent", "payload": {"path": "app.py"}},
            {"sequence": 5, "type": "approval_requested", "agent": "main_agent", "payload": {"tool": "run_bash"}},
            {"sequence": 6, "type": "approval_decided", "agent": "main_agent", "payload": {"tool": "run_bash", "approved": False}},
            {
                "sequence": 7,
                "type": "profile_switched",
                "agent": "main_agent",
                "payload": {"previous_profile": "coding-agent", "profile": "plan", "reason": "slash command"},
            },
            {"sequence": 8, "type": "plan_ready", "agent": "main_agent", "payload": {"profile": "plan"}},
            {"sequence": 9, "type": "task_outcome", "agent": "main_agent", "payload": {"status": "success", "summary": "done"}},
            {"sequence": 10, "type": "session_finished", "agent": "main_agent", "payload": {"status": "closed", "reason": "user_exit"}},
        ]

        summary = format_session_summary(metadata, events)

        self.assertIn("Session summary", summary)
        self.assertIn("id: session-a", summary)
        self.assertIn("status: closed", summary)
        self.assertIn("forked_from: session-parent", summary)
        self.assertIn("turns: 1 started, 0 finished", summary)
        self.assertIn("tools: 1 call(s): write_file=1", summary)
        self.assertIn("changed_files: app.py", summary)
        self.assertIn("approvals: 1 requested, 0 approved, 1 denied", summary)
        self.assertIn("profile_switches: coding-agent -> plan (slash command)", summary)
        self.assertIn("plans_ready: 1", summary)
        self.assertIn("task_outcome: success - done", summary)
        self.assertIn("recent_events:", summary)

    def test_session_summary_handles_empty_or_sparse_events(self):
        from harness_code_agent.sessions.summary import format_session_summary

        summary = format_session_summary(
            {"id": "empty-session", "profile": "plan", "status": "running"},
            [{"type": "after_tool", "payload": None}],
        )

        self.assertIn("id: empty-session", summary)
        self.assertIn("profile: plan", summary)
        self.assertIn("events: 1", summary)
        self.assertIn("tools: 1 call(s): unknown=1", summary)
        self.assertIn("changed_files: unknown", summary)
        self.assertIn("task_outcome: unknown", summary)

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
            event_types = [event["type"] for event in events]
            self.assertIn("tool_call", event_types)
            self.assertIn("tool_result", event_types)
            self.assertIn("file_changed", event_types)
            self.assertIn("file_change", event_types)
            self.assertIn("failure", event_types)
            denied_result = [
                event for event in events
                if event["type"] == "tool_result" and event["payload"].get("tool") == "run_bash"
            ][0]
            self.assertEqual(denied_result["payload"]["status"], "failed")
            self.assertFalse(denied_result["payload"]["ok"])
            approval = [
                event for event in events
                if event["type"] == "approval_decided" and event["payload"].get("tool") == "run_bash"
            ][0]
            self.assertFalse(approval["payload"]["approved"])

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
            requested = [event for event in events if event["type"] == "approval_requested"][0]
            decided = [event for event in events if event["type"] == "approval_decided"][0]
            tool_result = [event for event in events if event["type"] == "tool_result"][0]
            self.assertEqual(requested["payload"]["tool"], "write_file")
            self.assertEqual(decided["payload"]["tool"], "write_file")
            self.assertTrue(decided["payload"]["approved"])
            self.assertEqual(tool_result["payload"]["status"], "success")
            self.assertTrue(tool_result["payload"]["ok"])


if __name__ == "__main__":
    unittest.main()


