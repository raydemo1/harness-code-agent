"""PermissionMiddleware — permission/approval orchestration as a before_tool middleware.

This middleware is the single point of permission enforcement in the agent loop.
It replaces the permission/approval logic previously embedded in execute_tool_result().

Design:
  - PermissionPolicy: pure rule — tool + args → allow / ask / deny
  - ApprovalProvider: user interaction adapter — ApprovalRequest → ApprovalResult
  - PermissionMiddleware: orchestrates policy + provider in before_tool

The middleware holds a reference to ToolContext so that dynamic permission mode
changes (e.g. InteractiveSession.set_permission_mode()) are automatically reflected.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .approvals import ApprovalRequest
from .middleware import AgentMiddleware

if TYPE_CHECKING:
    from .tool_context import ToolContext

log = logging.getLogger("harness")


class PermissionMiddleware(AgentMiddleware):
    """Enforce permission policy and approval flow as a before_tool middleware.

    Parameters
    ----------
    tool_context : ToolContext
        Shared context holding the live ``permission_policy``, ``approval_provider``,
        and ``event_bus``.  Holding a reference (rather than copying values) ensures
        that runtime changes (e.g. toggling permission mode) take effect immediately.
    tool_registry : ToolRegistry
        Used to look up the declared permission level for each tool.
    """

    def __init__(self, tool_context: ToolContext, tool_registry):
        self._ctx = tool_context
        self._registry = tool_registry

    # ------------------------------------------------------------------
    # before_tool — the only hook this middleware needs
    # ------------------------------------------------------------------

    def before_tool(
        self,
        tool_name: str,
        tool_args: dict,
        messages: list[dict],
        runtime_state=None,
        agent_name: str | None = None,
    ) -> str | None:
        """Check permission policy; request user approval when needed.

        Returns
        -------
        str | None
            A blocking message (used as the tool result) if the call is
            denied or the user rejects approval.  ``None`` means "allow".
        """
        registry = self._ctx.tool_registry or self._registry
        decision = self._ctx.permission_policy.decide_tool_call(
            tool_name,
            tool_args,
            tool_permission=registry.permission_for(tool_name),
        )

        # --- deny (hard block, e.g. blacklisted shell command) ---
        if not decision.allowed and not decision.requires_approval:
            log.info(
                "PermissionMiddleware: blocked %s (risk=%s, reason=%s)",
                tool_name, decision.risk, decision.reason,
            )
            if tool_name == "run_bash" and decision.risk == "shell_blocked":
                return (
                    "[blocked] 工具未执行：该命令被安全黑名单拦截，可能造成不可恢复的数据或系统破坏。"
                    "请向用户明确说明未执行，并建议先备份或改用可恢复方案。"
                )
            if tool_name == "run_bash" and decision.risk == "shell_write_blocked":
                return (
                    "[blocked] 工具未执行：检测到直接删除或覆盖文件的命令。"
                    "当前策略不允许通过 shell 执行这类不可回退修改；请向用户明确说明未执行，"
                    "并建议先确认备份后改用受控文件编辑方式。"
                )
            return f"[blocked] {decision.reason}"

        # --- ask (requires user approval) ---
        if decision.requires_approval:
            redacted_args = _redact_tool_args(tool_args)
            approval_request = ApprovalRequest(
                tool_name=tool_name,
                args=redacted_args,
                risk=decision.risk,
                reason=decision.reason,
                agent_name=agent_name,
                session_id=self._ctx.session_id,
            )
            self._ctx.event_bus.emit(
                "approval_requested",
                agent=agent_name,
                payload={
                    "tool": tool_name,
                    "risk": decision.risk,
                    "reason": decision.reason,
                    "args": redacted_args,
                },
            )

            approval_result = self._ctx.approval_provider.request(approval_request)

            self._ctx.event_bus.emit(
                "approval_decided",
                agent=agent_name,
                payload={
                    "tool": tool_name,
                    "approved": approval_result.approved,
                    "reason": approval_result.reason,
                    "metadata": approval_result.metadata,
                },
            )

            if not approval_result.approved:
                log.info(
                    "PermissionMiddleware: user denied %s (reason=%s)",
                    tool_name, approval_result.reason,
                )
                return (
                    f"[approval_denied] 工具未执行：该操作未获得审批（{approval_result.reason}）。"
                    "请向用户明确说明未执行，不要假设文件或外部状态已经改变；如需继续，请先确认操作范围并准备备份。"
                )

        # --- allow ---
        return None


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _redact_tool_args(arguments: dict) -> dict:
    """Redact large argument values for display/logging."""
    redacted = dict(arguments or {})
    if "content" in redacted:
        redacted["content"] = f"[{len(str(redacted['content']))} chars]"
    return redacted
