from __future__ import annotations

from dataclasses import dataclass

from .approvals import ApprovalProvider, NoApprovalProvider
from .questions import NoQuestionProvider, QuestionProvider
from ..sessions.events import EventBus
from .permissions import PermissionPolicy
from ..workspace.service import WorkspaceService


@dataclass
class ToolContext:
    workspace: WorkspaceService
    permission_policy: PermissionPolicy
    event_bus: EventBus
    session_id: str | None = None
    approval_provider: ApprovalProvider = NoApprovalProvider()
    question_provider: QuestionProvider = NoQuestionProvider()
