"""Compatibility facade for the legacy runtime.tools module.

Production code should import narrower modules such as runtime.tool_registry,
runtime.tool_runner, or runtime.builtins.*. This module keeps the historical
public surface available for tests, users, and older integrations.
"""
from __future__ import annotations

import os

from .. import config
from ..agent.consultation import (
    CONSULTATION_SCOPES,
    ConsultationReadOnlyMiddleware,
    consult_subagent,
    consultation_tool_schemas,
)
from .builtins.browser import browser_test, stop_dev_server
from .builtins.discovery import tool_search
from .builtins.filesystem import (
    READ_FILE_MAX_LINES,
    READ_FILE_MAX_OUTPUT_CHARS,
    _resolve,
    apply_patch,
    list_files,
    read_file,
    read_skill_file,
    write_file,
)
from .builtins.interaction import ask_user
from .builtins.memory_tools import memory_search, read_memory_file, remember_memory
from .builtins.planning import update_plan_state
from .builtins.registry import BUILTIN_TOOL_REGISTRY, TOOL_DISPATCH, TOOL_SCHEMAS
from .builtins.schemas import BROWSER_TOOL_SCHEMAS, CORE_TOOL_SCHEMAS
from .builtins.shell import (
    _smart_truncate_output,
    list_shell_jobs,
    read_shell_output,
    run_bash,
    stop_shell_job,
)
from .builtins.web import web_fetch, web_search
from .tool_registry import (
    ToolExecutionLane,
    ToolRegistry,
    ToolSpec,
    tool_schemas_for_profile,
)
from .tool_runner import (
    _registry_for_context,
    _validate_and_fix,
    execute_tool,
    execute_tool_result,
    finalize_executed_tool_result,
    finalize_intercepted_tool_result,
)
from .tool_result import ToolResult
