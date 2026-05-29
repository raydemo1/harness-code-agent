from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .approvals import ApprovalProvider, NoApprovalProvider
from .questions import NoQuestionProvider, QuestionProvider
from ..sessions.events import EventBus
from .permissions import PermissionPolicy
from ..workspace.service import WorkspaceService

if TYPE_CHECKING:
    from .tools import ToolRegistry


@dataclass
class ToolContext:
    workspace: WorkspaceService
    permission_policy: PermissionPolicy
    event_bus: EventBus
    session_id: str | None = None
    approval_provider: ApprovalProvider = NoApprovalProvider()
    question_provider: QuestionProvider = NoQuestionProvider()
    tool_registry: ToolRegistry | None = None

