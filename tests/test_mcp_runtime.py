import json
import socket
import subprocess
import sys
import time
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class McpRuntimeTests(unittest.TestCase):
    def test_load_mcp_config_resolves_env_and_defaults_missing_permission_to_dangerous(self):
        from harness_code_agent.runtime.mcp import load_mcp_config

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".harness").mkdir()
            (root / ".harness" / "mcp.json").write_text(
                json.dumps(
                    {
                        "servers": {
                            "docs": {
                                "enabled": True,
                                "transport": "streamable_http",
                                "url": "https://example.test/mcp",
                                "headers": {"Authorization": "Bearer ${DOCS_TOKEN}"},
                                "permission": "network_read",
                            },
                            "local": {
                                "transport": "stdio",
                                "command": "python",
                                "args": ["server.py"],
                                "env": {"TOKEN": "${LOCAL_TOKEN}"},
                                "tool_permissions": {"search": "read"},
                            },
                            "off": {
                                "enabled": False,
                                "transport": "stdio",
                                "command": "python",
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )

            with patch.dict("os.environ", {"DOCS_TOKEN": "doc-secret", "LOCAL_TOKEN": "local-secret"}):
                cfg = load_mcp_config(root)

        self.assertEqual(set(cfg.servers), {"docs", "local"})
        self.assertEqual(cfg.servers["docs"].headers["Authorization"], "Bearer doc-secret")
        self.assertEqual(cfg.servers["docs"].permission, "network_read")
        self.assertEqual(cfg.servers["local"].permission, "dangerous")
        self.assertEqual(cfg.servers["local"].env["TOKEN"], "local-secret")
        self.assertEqual(cfg.servers["local"].tool_permissions["search"], "read")

    def test_load_mcp_config_rejects_invalid_permission_and_server_name(self):
        from harness_code_agent.runtime.mcp import McpConfigError, load_mcp_config

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".harness").mkdir()
            path = root / ".harness" / "mcp.json"
            path.write_text(
                json.dumps({"servers": {"bad name": {"transport": "stdio", "command": "python"}}}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(McpConfigError, "server name"):
                load_mcp_config(root)

            path.write_text(
                json.dumps({"servers": {"docs": {"transport": "stdio", "command": "python", "permission": "safeish"}}}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(McpConfigError, "permission"):
                load_mcp_config(root)

    def test_mcp_tools_register_only_on_session_registry_and_execute_via_context(self):
        from harness_code_agent.runtime import tools
        from harness_code_agent.runtime.mcp import McpToolBinding
        from harness_code_agent.runtime.permissions import PermissionPolicy
        from harness_code_agent.runtime.tool_context import ToolContext
        from harness_code_agent.sessions.events import EventBus
        from harness_code_agent.workspace.service import WorkspaceService

        class FakeMcpManager:
            def __init__(self):
                self.calls = []
                self.tool_bindings = [
                    McpToolBinding(
                        exposed_name="mcp__docs__search",
                        server_name="docs",
                        tool_name="search",
                        description="Search docs",
                        input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
                        permission="read",
                    )
                ]

            def call_tool(self, exposed_name, arguments):
                self.calls.append((exposed_name, arguments))
                return tools.ToolResult(
                    tool=exposed_name,
                    status="success",
                    output=f"searched {arguments['query']}",
                    metadata={"status_source": "mcp"},
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = tools.BUILTIN_TOOL_REGISTRY.copy()
            manager = FakeMcpManager()
            manager.register_tools = lambda target: [
                target.register(binding.schema(), lambda **kwargs: manager.call_tool(
                    binding.exposed_name,
                    {k: v for k, v in kwargs.items() if k not in {"runtime_state", "agent_name", "tool_context"}},
                ), permission=binding.permission)
                for binding in manager.tool_bindings
            ]
            manager.register_tools(registry)
            context = ToolContext(
                workspace=WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots"),
                permission_policy=PermissionPolicy(mode="workspace-write"),
                event_bus=EventBus(),
                tool_registry=registry,
            )

            result = tools.execute_tool_result("mcp__docs__search", {"query": "agent"}, tool_context=context)

        self.assertIsNone(tools.BUILTIN_TOOL_REGISTRY.get("mcp__docs__search"))
        self.assertEqual(result.status, "success")
        self.assertEqual(result.output, "searched agent")
        self.assertEqual(manager.calls, [("mcp__docs__search", {"query": "agent"})])

    def test_mcp_result_conversion_preserves_text_structured_content_and_errors(self):
        from harness_code_agent.runtime.mcp import mcp_result_to_tool_result

        ok = SimpleNamespace(
            content=[
                SimpleNamespace(type="text", text="plain text"),
                SimpleNamespace(type="image", mimeType="image/png", data="abc123"),
            ],
            structuredContent={"answer": 42},
            isError=False,
        )
        failed = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="boom")],
            structuredContent=None,
            isError=True,
        )

        ok_result = mcp_result_to_tool_result("mcp__docs__lookup", ok)
        failed_result = mcp_result_to_tool_result("mcp__docs__lookup", failed)

        self.assertEqual(ok_result.status, "success")
        self.assertIn("plain text", ok_result.output)
        self.assertIn("[image image/png", ok_result.output)
        self.assertIn('"answer": 42', ok_result.output)
        self.assertEqual(failed_result.status, "failed")
        self.assertEqual(failed_result.error, "MCP tool returned isError=true")

    def test_permission_middleware_uses_session_registry_for_mcp_tool_permissions(self):
        from harness_code_agent.runtime import tools
        from harness_code_agent.runtime.permission_middleware import PermissionMiddleware
        from harness_code_agent.runtime.permissions import PermissionPolicy
        from harness_code_agent.runtime.tool_context import ToolContext
        from harness_code_agent.sessions.events import EventBus
        from harness_code_agent.workspace.service import WorkspaceService

        schema = {
            "type": "function",
            "function": {
                "name": "mcp__danger__delete",
                "description": "Delete remotely",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        registry = tools.BUILTIN_TOOL_REGISTRY.copy()
        registry.register(schema, lambda **_: "deleted", permission="dangerous")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = ToolContext(
                workspace=WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots"),
                permission_policy=PermissionPolicy(mode="workspace-write"),
                event_bus=EventBus(),
                tool_registry=registry,
            )
            middleware = PermissionMiddleware(tool_context=context, tool_registry=tools.BUILTIN_TOOL_REGISTRY)

            blocked = middleware.before_tool("mcp__danger__delete", {}, messages=[], agent_name="main_agent")

        self.assertIn("[approval_denied]", blocked)

    def test_profile_schema_filtering_applies_to_dynamic_mcp_permissions(self):
        from harness_code_agent.runtime import tools

        registry = tools.BUILTIN_TOOL_REGISTRY.copy()
        registry.register(
            {
                "type": "function",
                "function": {
                    "name": "mcp__docs__search",
                    "description": "Search docs",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            lambda **_: "ok",
            permission="read",
        )
        registry.register(
            {
                "type": "function",
                "function": {
                    "name": "mcp__danger__delete",
                    "description": "Delete remote data",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            lambda **_: "ok",
            permission="dangerous",
        )

        names = {
            schema["function"]["name"]
            for schema in tools.tool_schemas_for_profile(
                allowed_permissions={"read", "network_read", "control"},
                registry=registry,
            )
        }

        self.assertIn("mcp__docs__search", names)
        self.assertNotIn("mcp__danger__delete", names)

    def test_doctor_reports_mcp_config_errors_without_starting_a_session(self):
        from harness_code_agent.core.interactive import format_doctor

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".harness").mkdir()
            (root / ".harness" / "mcp.json").write_text(
                json.dumps({"servers": {"bad name": {"transport": "stdio", "command": "python"}}}),
                encoding="utf-8",
            )

            text, failures = format_doctor(root)

        self.assertGreaterEqual(failures, 1)
        self.assertIn("MCP", text)
        self.assertIn("Invalid MCP server name", text)

    def test_stdio_mcp_server_tool_is_registered_and_callable(self):
        from harness_code_agent.runtime import tools
        from harness_code_agent.runtime.mcp import McpClientManager
        from harness_code_agent.runtime.permissions import PermissionPolicy
        from harness_code_agent.runtime.tool_context import ToolContext
        from harness_code_agent.sessions.events import EventBus
        from harness_code_agent.workspace.service import WorkspaceService

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".harness").mkdir()
            server = root / "stdio_server.py"
            server.write_text(
                "\n".join(
                    [
                        "from mcp.server.fastmcp import FastMCP",
                        "mcp = FastMCP('stdio-test', log_level='ERROR')",
                        "@mcp.tool()",
                        "def echo(text: str) -> str:",
                        "    return 'echo:' + text",
                        "if __name__ == '__main__':",
                        "    mcp.run()",
                    ]
                ),
                encoding="utf-8",
            )
            (root / ".harness" / "mcp.json").write_text(
                json.dumps(
                    {
                        "servers": {
                            "local": {
                                "transport": "stdio",
                                "command": sys.executable,
                                "args": [str(server)],
                                "permission": "read",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            manager = McpClientManager.from_workspace(root)
            try:
                manager.connect_all()
                registry = tools.BUILTIN_TOOL_REGISTRY.copy()
                manager.register_tools(registry)
                context = ToolContext(
                    workspace=WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots"),
                    permission_policy=PermissionPolicy(mode="workspace-write"),
                    event_bus=EventBus(),
                    tool_registry=registry,
                )

                result = tools.execute_tool_result(
                    "mcp__local__echo",
                    {"text": "hello"},
                    tool_context=context,
                    agent_name="main_agent",
                )
            finally:
                manager.close()

        self.assertEqual(result.status, "success")
        self.assertIn("echo:hello", result.output)

    def test_streamable_http_mcp_server_tool_is_registered_and_callable(self):
        from harness_code_agent.runtime import tools
        from harness_code_agent.runtime.mcp import McpClientManager
        from harness_code_agent.runtime.permissions import PermissionPolicy
        from harness_code_agent.runtime.tool_context import ToolContext
        from harness_code_agent.sessions.events import EventBus
        from harness_code_agent.workspace.service import WorkspaceService

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".harness").mkdir()
            port = _free_port()
            server = root / "http_server.py"
            server.write_text(
                "\n".join(
                    [
                        "from mcp.server.fastmcp import FastMCP",
                        f"mcp = FastMCP('http-test', host='127.0.0.1', port={port}, log_level='ERROR')",
                        "@mcp.tool()",
                        "def add(a: int, b: int) -> int:",
                        "    return a + b",
                        "if __name__ == '__main__':",
                        "    mcp.run('streamable-http')",
                    ]
                ),
                encoding="utf-8",
            )
            process = subprocess.Popen(
                [sys.executable, str(server)],
                cwd=root,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                _wait_for_port(port)
                (root / ".harness" / "mcp.json").write_text(
                    json.dumps(
                        {
                            "servers": {
                                "remote": {
                                    "transport": "streamable_http",
                                    "url": f"http://127.0.0.1:{port}/mcp",
                                    "permission": "network_read",
                                }
                            }
                        }
                    ),
                    encoding="utf-8",
                )

                manager = McpClientManager.from_workspace(root)
                try:
                    manager.connect_all()
                    registry = tools.BUILTIN_TOOL_REGISTRY.copy()
                    manager.register_tools(registry)
                    context = ToolContext(
                        workspace=WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots"),
                        permission_policy=PermissionPolicy(mode="workspace-write"),
                        event_bus=EventBus(),
                        tool_registry=registry,
                    )

                    result = tools.execute_tool_result(
                        "mcp__remote__add",
                        {"a": 2, "b": 5},
                        tool_context=context,
                        agent_name="main_agent",
                    )
                finally:
                    manager.close()
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()

        self.assertEqual(result.status, "success")
        self.assertIn("7", result.output)

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_port(port: int, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    last_error: OSError | None = None
    while time.time() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.2)
            try:
                sock.connect(("127.0.0.1", port))
                return
            except OSError as exc:
                last_error = exc
        time.sleep(0.1)
    raise TimeoutError(f"Timed out waiting for port {port}: {last_error}")


if __name__ == "__main__":
    unittest.main()
