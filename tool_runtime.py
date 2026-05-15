from __future__ import annotations

from dataclasses import dataclass

from approvals import ApprovalProvider, NoApprovalProvider
from events import EventBus
from permissions import PermissionPolicy
from workspace_service import WorkspaceService


@dataclass
class ToolContext:
    workspace: WorkspaceService
    permission_policy: PermissionPolicy
    event_bus: EventBus
    session_id: str | None = None
    approval_provider: ApprovalProvider = NoApprovalProvider()
