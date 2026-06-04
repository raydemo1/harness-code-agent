"""Read-only consultation sub-agent tool."""
from __future__ import annotations

import json

from ..runtime.builtins.schemas import CORE_TOOL_SCHEMAS
from ..runtime.middleware import AgentMiddleware
from ..runtime.permissions import is_read_only_command
from ..runtime.tool_result import ToolResult


CONSULTATION_SCOPES = {
    "codebase_investigation",
    "parallel_search",
    "test_design",
    "review",
}


def _tool_schema_by_name(name: str) -> dict | None:
    for schema in CORE_TOOL_SCHEMAS:
        if schema.get("function", {}).get("name") == name:
            return schema
    return None


def consultation_tool_schemas() -> list[dict]:
    """Return the read-only and verification tool surface for consultation sub-agents."""
    names = ("read_file", "list_files", "run_bash", "web_search", "web_fetch")
    return [schema for name in names if (schema := _tool_schema_by_name(name)) is not None]


class ConsultationReadOnlyMiddleware(AgentMiddleware):
    """Allow consultation helpers to inspect and verify without modifying state."""

    _WRITE_OR_CONTROL_TOOLS = {
        "write_file",
        "apply_patch",
        "update_plan_state",
        "ask_user",
        "browser_test",
        "stop_dev_server",
        "stop_shell_job",
    }

    def before_tool(
        self,
        tool_name: str,
        tool_args: dict,
        messages: list[dict],
        runtime_state=None,
        agent_name: str | None = None,
    ) -> ToolResult | None:
        if tool_name in self._WRITE_OR_CONTROL_TOOLS:
            return ToolResult(
                tool=tool_name,
                status="failed",
                output="[blocked] Consultation sub-agents are read-only and must not modify files or control workflow state.",
                error="consultation sub-agent is read-only",
                metadata={"status_source": "consultation_policy"},
            )
        if tool_name == "run_bash":
            command = str(tool_args.get("command", ""))
            if not is_read_only_command(command):
                return ToolResult(
                    tool=tool_name,
                    status="failed",
                    output="[blocked] Consultation sub-agents may only run read-only or verification shell commands.",
                    error="only read-only or verification shell commands are allowed",
                    metadata={"status_source": "consultation_policy"},
                )
        return None


def _as_consultation_report(scope: str, raw_result: str) -> str:
    raw_result = raw_result or ""
    try:
        parsed = json.loads(raw_result)
        if isinstance(parsed, dict) and {"status", "scope", "findings", "evidence", "recommendations", "risks"} <= set(parsed):
            report = parsed
        else:
            raise ValueError("not a consultation report")
    except Exception:
        report = {
            "status": "completed" if raw_result.strip() else "blocked",
            "scope": scope,
            "findings": [raw_result[:7000]] if raw_result.strip() else [],
            "evidence": [],
            "recommendations": [],
            "risks": [],
        }

    text = json.dumps(report, ensure_ascii=False)
    if len(text) > 8000:
        report["findings"] = [
            "\n".join(str(item) for item in report.get("findings", []))[:7000]
            + "\n...(truncated)"
        ]
        text = json.dumps(report, ensure_ascii=False)
    return text


def consult_subagent(task: str, scope: str = "codebase_investigation") -> ToolResult:
    """
    Ask a read-only consultation sub-agent for local findings.

    The main agent owns all code changes, final integration, verification, and
    stopping decisions. Consultation sub-agents may only investigate, search,
    suggest tests, or review; they return a structured report.
    """
    if scope not in CONSULTATION_SCOPES:
        output = "[error] Invalid consultation scope. Use one of: " + ", ".join(sorted(CONSULTATION_SCOPES))
        return ToolResult(
            tool="consult_subagent",
            status="failed",
            output=output,
            error=output.removeprefix("[error] "),
            metadata={"scope": scope, "status_source": "validation"},
        )

    from .conversation import Agent

    sub = Agent(
        name=f"consult_{scope}",
        system_prompt=(
            "You are a read-only consultation helper. You are not a separate implementation owner.\n"
            "You may inspect files, run read-only or verification shell commands, search, and fetch references. "
            "Use shell commands for evidence such as git diff, git status, pytest, ruff check, or similar checks. "
            "You must not modify files, start services, install packages, change git state, "
            "or decide whether the overall task is complete.\n"
            "Return only JSON with this shape:\n"
            "{\n"
            '  "status": "completed | blocked",\n'
            f'  "scope": "{scope}",\n'
            '  "findings": ["..."],\n'
            '  "evidence": ["file/path.py:line or command output summary"],\n'
            '  "recommendations": ["..."],\n'
            '  "risks": ["..."]\n'
            "}\n"
            "For test_design scope, provide test cases and assertions only; do not write tests."
        ),
        use_tools=True,
        tool_schemas=consultation_tool_schemas(),
        middlewares=[ConsultationReadOnlyMiddleware()],
    )

    result = sub.run(task)
    return ToolResult(
        tool="consult_subagent",
        status="success",
        output=_as_consultation_report(scope, result),
        metadata={"scope": scope, "status_source": "native"},
    )
