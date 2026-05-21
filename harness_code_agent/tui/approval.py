from __future__ import annotations

from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.formatted_text import HTML

from ..runtime.approvals import ApprovalRequest, ApprovalResult


class TuiApprovalProvider:
    def __init__(self):
        self.prompt_session = PromptSession()

    def request(self, request: ApprovalRequest) -> ApprovalResult:
        print_formatted_text(HTML("\n<ansimagenta><b>Approval required</b></ansimagenta>"))
        print_formatted_text(f"tool: {request.tool_name}")
        print_formatted_text(f"risk: {request.risk}")
        print_formatted_text(f"reason: {request.reason}")
        if request.tool_name == "run_bash":
            print_formatted_text(f"command: {request.args.get('command', '')}")
        else:
            print_formatted_text(f"args: {_summarize_args(request.args)}")
        while True:
            answer = self.prompt_session.prompt("Approve? [y/N/detail] ").strip().lower()
            if answer in {"y", "yes"}:
                return ApprovalResult(True, "approved in TUI", {"ui": "tui"})
            if answer in {"", "n", "no"}:
                return ApprovalResult(False, "denied in TUI", {"ui": "tui"})
            if answer in {"d", "detail", "details"}:
                print_formatted_text(repr(request.args))


def _summarize_args(args: dict) -> dict:
    summary = dict(args or {})
    if "content" in summary:
        summary["content"] = f"[{len(str(summary['content']))} chars]"
    return summary
