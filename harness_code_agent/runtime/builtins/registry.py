"""Composition root for built-in tool registration."""
from __future__ import annotations

from ...agent.consultation import consult_subagent
from ..permissions import (
    TOOL_PERMISSION_CONTROL,
    TOOL_PERMISSION_EDIT,
    TOOL_PERMISSION_NETWORK_READ,
    TOOL_PERMISSION_READ,
    TOOL_PERMISSION_SHELL,
)
from ..tool_registry import ToolExecutionLane, ToolRegistry
from .browser import browser_test, stop_dev_server
from .discovery import tool_search
from .filesystem import apply_patch, list_files, read_file, read_skill_file, repo_search, write_file
from .interaction import ask_user
from .memory_tools import memory_search, read_memory_file, remember_memory
from .parallel import parallel
from .planning import update_plan_state
from .schemas import BROWSER_TOOL_SCHEMAS, CORE_TOOL_SCHEMAS
from .shell import list_shell_jobs, read_shell_output, run_bash, stop_shell_job
from .web import web_fetch, web_search


def _build_builtin_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    handlers = {
        "read_file": read_file,
        "read_skill_file": read_skill_file,
        "repo_search": repo_search,
        "tool_search": tool_search,
        "parallel": parallel,
        "write_file": write_file,
        "apply_patch": apply_patch,
        "update_plan_state": update_plan_state,
        "list_files": list_files,
        "ask_user": ask_user,
        "memory_search": memory_search,
        "remember_memory": remember_memory,
        "read_memory_file": read_memory_file,
        "run_bash": run_bash,
        "list_shell_jobs": list_shell_jobs,
        "read_shell_output": read_shell_output,
        "stop_shell_job": stop_shell_job,
        "consult_subagent": consult_subagent,
        "web_search": web_search,
        "web_fetch": web_fetch,
        "browser_test": browser_test,
        "stop_dev_server": stop_dev_server,
    }
    permissions = {
        "read_file": TOOL_PERMISSION_READ,
        "read_skill_file": TOOL_PERMISSION_READ,
        "repo_search": TOOL_PERMISSION_READ,
        "tool_search": TOOL_PERMISSION_READ,
        "parallel": TOOL_PERMISSION_READ,
        "list_files": TOOL_PERMISSION_READ,
        "ask_user": TOOL_PERMISSION_READ,
        "memory_search": TOOL_PERMISSION_READ,
        "remember_memory": TOOL_PERMISSION_EDIT,
        "read_memory_file": TOOL_PERMISSION_READ,
        "consult_subagent": TOOL_PERMISSION_READ,
        "web_search": TOOL_PERMISSION_NETWORK_READ,
        "web_fetch": TOOL_PERMISSION_NETWORK_READ,
        "write_file": TOOL_PERMISSION_EDIT,
        "apply_patch": TOOL_PERMISSION_EDIT,
        "update_plan_state": TOOL_PERMISSION_CONTROL,
        "run_bash": TOOL_PERMISSION_SHELL,
        "list_shell_jobs": TOOL_PERMISSION_READ,
        "read_shell_output": TOOL_PERMISSION_READ,
        "stop_shell_job": TOOL_PERMISSION_CONTROL,
        "browser_test": TOOL_PERMISSION_SHELL,
        "stop_dev_server": TOOL_PERMISSION_SHELL,
    }
    lanes = {
        "read_file": ToolExecutionLane.WORKSPACE_READ,
        "read_skill_file": ToolExecutionLane.WORKSPACE_READ,
        "repo_search": ToolExecutionLane.WORKSPACE_READ,
        "tool_search": ToolExecutionLane.CONTROL_SERIAL,
        "parallel": ToolExecutionLane.WORKSPACE_READ,
        "list_files": ToolExecutionLane.WORKSPACE_READ,
        "ask_user": ToolExecutionLane.CONTROL_SERIAL,
        "memory_search": ToolExecutionLane.WORKSPACE_READ,
        "remember_memory": ToolExecutionLane.WORKSPACE_WRITE,
        "read_memory_file": ToolExecutionLane.WORKSPACE_READ,
        "consult_subagent": ToolExecutionLane.SUBAGENT_READ,
        "web_search": ToolExecutionLane.NETWORK_READ,
        "web_fetch": ToolExecutionLane.NETWORK_READ,
        "write_file": ToolExecutionLane.WORKSPACE_WRITE,
        "apply_patch": ToolExecutionLane.WORKSPACE_WRITE,
        "update_plan_state": ToolExecutionLane.CONTROL_SERIAL,
        "run_bash": ToolExecutionLane.SHELL_SERIAL,
        "list_shell_jobs": ToolExecutionLane.CONTROL_SERIAL,
        "read_shell_output": ToolExecutionLane.CONTROL_SERIAL,
        "stop_shell_job": ToolExecutionLane.CONTROL_SERIAL,
        "browser_test": ToolExecutionLane.SHELL_SERIAL,
        "stop_dev_server": ToolExecutionLane.SHELL_SERIAL,
    }
    for schema in CORE_TOOL_SCHEMAS + BROWSER_TOOL_SCHEMAS:
        name = schema["function"]["name"]
        if name in handlers:
            registry.register(schema, handlers[name], permission=permissions.get(name), lane=lanes.get(name))
    return registry


BUILTIN_TOOL_REGISTRY = _build_builtin_tool_registry()
TOOL_SCHEMAS = CORE_TOOL_SCHEMAS
TOOL_DISPATCH = BUILTIN_TOOL_REGISTRY.dispatch()
