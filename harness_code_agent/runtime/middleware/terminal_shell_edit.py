"""Terminal-only policy that keeps source edits in structured file tools."""
from __future__ import annotations

import re

from .base import AgentMiddleware, MAIN_AGENT_NAMES


_EDIT_COMMAND_PATTERNS = (
    r"(?:^|[;&|]\s*)(?:set-content|add-content|out-file|tee-object|tee)\b",
    r"(?:^|[;&|]\s*)(?:new-item|remove-item|move-item|copy-item)\b",
    r"(?:^|[;&|]\s*)(?:rm|mv|cp|touch|truncate|del|erase|patch|sponge)\b",
    r"(?:^|[;&|]\s*)git\s+(?:apply|checkout|restore|reset|mv|rm)\b",
    r"(?:^|[;&|]\s*)sed\b[^;&|]*\s-i(?:\s|$)",
    r"(?:^|[;&|]\s*)perl\b[^;&|]*\s-pi(?:\s|$)",
    r"(?:^|[;&|]\s*)prettier\b[^;&|]*--write\b",
    r"(?:^|[;&|]\s*)gofmt\b[^;&|]*\s-w(?:\s|$)",
    r"(?:^|[;&|]\s*)(?:ruff\s+check|eslint)\b[^;&|]*--fix\b",
    r"\b(?:write_text|write_bytes)\s*\(",
    r"\bopen\s*\([^)]*,\s*['\"](?:w|a|x|\+)",
    r"\bfs\.(?:writefile|appendfile|rename|unlink)\w*\s*\(",
)


class TerminalShellEditPolicyMiddleware(AgentMiddleware):
    def before_tool(
        self,
        tool_name: str,
        tool_args: dict,
        messages: list[dict],
        runtime_state=None,
        agent_name: str | None = None,
    ) -> str | None:
        if agent_name not in MAIN_AGENT_NAMES or tool_name != "run_bash":
            return None
        command = str(tool_args.get("command") or "")
        if _contains_output_redirection(command) or _looks_like_formatter_edit(command) or any(
            re.search(pattern, command, flags=re.IGNORECASE)
            for pattern in _EDIT_COMMAND_PATTERNS
        ):
            return (
                "[blocked] Terminal profile does not allow explicit file editing through run_bash. "
                "Use write_file/apply_patch so edits remain observable."
            )
        return None


def _contains_output_redirection(command: str) -> bool:
    quote: str | None = None
    index = 0
    while index < len(command):
        char = command[index]
        if quote:
            if char == quote:
                quote = None
            elif char == "\\" and quote == '"' and index + 1 < len(command):
                index += 1
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == ">":
            remainder = command[index + 1 :].lstrip()
            if remainder.startswith("&") and remainder[1:2].isdigit():
                index += 1
                continue
            return True
        index += 1
    return False


def _looks_like_formatter_edit(command: str) -> bool:
    lowered = " ".join(command.lower().split())
    if re.search(r"(?:^|[;&|]\s*)black\b", lowered):
        return "--check" not in lowered and "--diff" not in lowered
    if re.search(r"(?:^|[;&|]\s*)ruff\s+format\b", lowered):
        return "--check" not in lowered
    if re.search(r"(?:^|[;&|]\s*)cargo\s+fmt\b", lowered):
        return "--check" not in lowered
    return False
