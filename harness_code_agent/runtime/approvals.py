from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class ApprovalRequest:
    tool_name: str
    args: dict
    risk: str
    reason: str
    agent_name: str | None = None
    session_id: str | None = None


@dataclass
class ApprovalResult:
    approved: bool
    reason: str
    metadata: dict = field(default_factory=dict)


class ApprovalProvider(Protocol):
    def request(self, request: ApprovalRequest) -> ApprovalResult:
        ...


class NoApprovalProvider:
    """Safe default for non-interactive tests and batch runs."""

    def request(self, request: ApprovalRequest) -> ApprovalResult:
        return ApprovalResult(False, "no approval provider configured")


class StaticApprovalProvider:
    """Deterministic provider for tests and scripted runs."""

    def __init__(self, *, approved: bool, reason: str = ""):
        self.approved = approved
        self.reason = reason or ("approved" if approved else "denied")

    def request(self, request: ApprovalRequest) -> ApprovalResult:
        return ApprovalResult(self.approved, self.reason)


class ConsoleApprovalProvider:
    """Prompt on stdin/stdout when an interactive terminal is available."""

    def request(self, request: ApprovalRequest) -> ApprovalResult:
        if not sys.stdin.isatty():
            return ApprovalResult(False, "approval requires an interactive terminal")
        print("\nApproval required for tool call:")
        print(f"  tool: {request.tool_name}")
        print(f"  risk: {request.risk}")
        print(f"  reason: {request.reason}")
        if request.tool_name == "run_bash":
            print(f"  command: {request.args.get('command', '')}")
        else:
            print(f"  args: {_summarize_args(request.args)}")
        answer = input("Approve this action? [y/N] ").strip().lower()
        if answer in {"y", "yes"}:
            return ApprovalResult(True, "approved by user")
        return ApprovalResult(False, "denied by user")


def _summarize_args(args: dict) -> dict:
    summary = dict(args or {})
    if "content" in summary:
        summary["content"] = f"[{len(str(summary['content']))} chars]"
    return summary
