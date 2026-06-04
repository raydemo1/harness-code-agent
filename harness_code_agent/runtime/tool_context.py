from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .approvals import ApprovalProvider, NoApprovalProvider
from .questions import NoQuestionProvider, QuestionProvider
from ..sessions.events import EventBus
from .permissions import PermissionPolicy
from ..workspace.service import WorkspaceService

if TYPE_CHECKING:
    from .tool_registry import ToolRegistry


@dataclass
class ToolContext:
    workspace: WorkspaceService
    permission_policy: PermissionPolicy
    event_bus: EventBus
    session_id: str | None = None
    approval_provider: ApprovalProvider = NoApprovalProvider()
    question_provider: QuestionProvider = NoQuestionProvider()
    tool_registry: ToolRegistry | None = None
    allowed_tool_permissions: set[str] | None = None
    blocked_tool_names: set[str] = field(default_factory=set)
    revealed_tool_names: set[str] = field(default_factory=set)
