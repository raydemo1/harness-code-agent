"""Compatibility facade for the legacy agent.loop module."""
from __future__ import annotations

from .. import config
from . import context
from .conversation import (
    Agent,
    AgentConversation,
    _assistant_message_from_response,
    _requires_reasoning_content_roundtrip,
    _safe_args_preview,
    _safe_tool_summary,
    _tool_names_from_schemas,
    _tool_result_from_before_tool_block,
    _truncate,
    llm_call_simple,
)
from .providers import get_client
from .runtime_state import AgentFallbackState, AgentRuntimeState, RecoveryState, TaskBoard
from .trace import TraceWriter
from ..runtime.arg_preview import safe_args_preview
