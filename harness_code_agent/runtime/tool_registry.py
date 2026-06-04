"""Tool registry and schema filtering primitives."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable

from .permissions import (
    TOOL_PERMISSION_CONTROL,
    TOOL_PERMISSION_EDIT,
    TOOL_PERMISSION_NETWORK_READ,
    TOOL_PERMISSION_READ,
    TOOL_PERMISSION_SHELL,
    VALID_TOOL_PERMISSIONS,
)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    schema: dict
    handler: Callable
    permission: str
    lane: "ToolExecutionLane"
    disclosure: str = "core"


class ToolExecutionLane(str, Enum):
    WORKSPACE_READ = "workspace_read"
    NETWORK_READ = "network_read"
    SUBAGENT_READ = "subagent_read"
    SHELL_READ = "shell_read"
    SHELL_VERIFY = "shell_verify"
    SHELL_LONG_RUNNING = "shell_long_running"
    WORKSPACE_WRITE = "workspace_write"
    CONTROL_SERIAL = "control_serial"
    SHELL_SERIAL = "shell_serial"
    BLOCKED = "blocked"


VALID_TOOL_DISCLOSURES = {"core", "deferred"}


class ToolRegistry:
    """Thin registry boundary for built-in and future profile-provided tools."""

    def __init__(self):
        self._schemas: dict[str, dict] = {}
        self._handlers: dict[str, Callable] = {}
        self._permissions: dict[str, str] = {}
        self._lanes: dict[str, ToolExecutionLane] = {}
        self._disclosures: dict[str, str] = {}

    def register(
        self,
        schema: dict,
        handler: Callable,
        *,
        permission: str | None = None,
        lane: ToolExecutionLane | str | None = None,
        disclosure: str = "core",
    ) -> None:
        name = schema.get("function", {}).get("name")
        if not name:
            raise ValueError("Tool schema missing function.name")
        if permission is None:
            raise ValueError(f"Tool {name} missing permission classification")
        if permission not in VALID_TOOL_PERMISSIONS:
            raise ValueError(f"Tool {name} has unknown permission classification: {permission}")
        if disclosure not in VALID_TOOL_DISCLOSURES:
            raise ValueError(f"Tool {name} has unknown disclosure classification: {disclosure}")
        self._schemas[name] = schema
        self._handlers[name] = handler
        self._permissions[name] = permission
        self._lanes[name] = _coerce_tool_lane(lane) if lane is not None else _default_lane_for_tool(name, permission)
        self._disclosures[name] = disclosure

    def get(self, name: str) -> Callable | None:
        return self._handlers.get(name)

    def permission_for(self, name: str) -> str | None:
        return self._permissions.get(name)

    def lane_for(self, name: str) -> ToolExecutionLane | None:
        return self._lanes.get(name)

    def disclosure_for(self, name: str) -> str | None:
        return self._disclosures.get(name)

    def specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name,
                self._schemas[name],
                self._handlers[name],
                self._permissions[name],
                self._lanes[name],
                self._disclosures.get(name, "core"),
            )
            for name in sorted(self._schemas)
        ]

    def schemas(self) -> list[dict]:
        return [self._schemas[name] for name in sorted(self._schemas)]

    def dispatch(self) -> dict[str, Callable]:
        return dict(self._handlers)

    def copy(self) -> "ToolRegistry":
        clone = ToolRegistry()
        clone._schemas = dict(self._schemas)
        clone._handlers = dict(self._handlers)
        clone._permissions = dict(self._permissions)
        clone._lanes = dict(self._lanes)
        clone._disclosures = dict(self._disclosures)
        return clone


def _coerce_tool_lane(lane: ToolExecutionLane | str) -> ToolExecutionLane:
    if isinstance(lane, ToolExecutionLane):
        return lane
    try:
        return ToolExecutionLane(str(lane))
    except ValueError as exc:
        raise ValueError(f"Unknown tool execution lane: {lane}") from exc


def _default_lane_for_tool(name: str, permission: str) -> ToolExecutionLane:
    if name in {"read_file", "list_files", "read_skill_file"}:
        return ToolExecutionLane.WORKSPACE_READ
    if name in {"web_search", "web_fetch"}:
        return ToolExecutionLane.NETWORK_READ
    if name == "consult_subagent":
        return ToolExecutionLane.SUBAGENT_READ
    if name in {"write_file", "apply_patch"}:
        return ToolExecutionLane.WORKSPACE_WRITE
    if name in {"ask_user", "update_plan_state"}:
        return ToolExecutionLane.CONTROL_SERIAL
    if name == "run_bash":
        return ToolExecutionLane.SHELL_SERIAL
    if name in {"list_shell_jobs", "read_shell_output", "stop_shell_job"}:
        return ToolExecutionLane.CONTROL_SERIAL
    if name in {"browser_test", "stop_dev_server"}:
        return ToolExecutionLane.SHELL_SERIAL
    if permission == TOOL_PERMISSION_NETWORK_READ:
        return ToolExecutionLane.NETWORK_READ
    if permission == TOOL_PERMISSION_READ:
        return ToolExecutionLane.WORKSPACE_READ
    if permission == TOOL_PERMISSION_EDIT:
        return ToolExecutionLane.WORKSPACE_WRITE
    if permission == TOOL_PERMISSION_SHELL:
        return ToolExecutionLane.SHELL_SERIAL
    return ToolExecutionLane.CONTROL_SERIAL


def tool_schemas_for_profile(
    *,
    allowed_permissions: set[str] | None = None,
    include_names: set[str] | None = None,
    exclude_names: set[str] | None = None,
    registry: ToolRegistry | None = None,
    disclosure: set[str] | None = None,
) -> list[dict]:
    if registry is None:
        from .builtins.registry import BUILTIN_TOOL_REGISTRY

        registry = BUILTIN_TOOL_REGISTRY
    allowed_permissions = set(allowed_permissions) if allowed_permissions is not None else None
    include_names = set(include_names or set())
    exclude_names = set(exclude_names or set())
    disclosures = {"core"} if disclosure is None else set(disclosure)
    if allowed_permissions is not None:
        unknown_permissions = allowed_permissions - VALID_TOOL_PERMISSIONS
        if unknown_permissions:
            names = ", ".join(sorted(unknown_permissions))
            raise ValueError(f"Unknown tool permission classification for profile: {names}")
    unknown_disclosures = disclosures - VALID_TOOL_DISCLOSURES
    if unknown_disclosures:
        names = ", ".join(sorted(unknown_disclosures))
        raise ValueError(f"Unknown tool disclosure classification for profile: {names}")

    schemas: list[dict] = []
    for spec in registry.specs():
        if spec.name in exclude_names:
            continue
        if spec.disclosure not in disclosures:
            continue
        if spec.name in include_names or (
            allowed_permissions is not None and spec.permission in allowed_permissions
        ):
            schemas.append(spec.schema)
    return schemas
