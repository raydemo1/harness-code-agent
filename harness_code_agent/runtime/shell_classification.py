from __future__ import annotations

import re
from typing import Literal


SafeShellCommandKind = Literal["read", "verify", "unsafe"]


_READ_COMMAND_PREFIXES = (
    "cat",
    "type",
    "ls",
    "dir",
    "pwd",
    "grep",
    "rg",
    "head",
    "tail",
    "git status",
    "git diff",
    "git log",
    "git show",
    "git branch",
    "test",
    "diff",
    "wc",
    "which",
    "where",
    "get-content",
    "gc",
    "select-string",
    "sls",
    "select-object",
    "sort-object",
    "sort",
    "measure-object",
    "measure",
    "out-string",
)

_VERIFY_COMMAND_PREFIXES = (
    "python -m unittest",
    "python -m pytest",
    "pytest",
    "ruff check",
    "mypy",
    "npm test",
    "go test",
    "cargo test",
    "tsc --noemit",
)

_WRITE_PIPELINE_COMMAND_PREFIXES = (
    "tee",
    "tee-object",
    "out-file",
    "set-content",
    "add-content",
    "new-item",
    "copy-item",
    "move-item",
    "remove-item",
)


def is_long_running_shell_command(command: str) -> bool:
    lowered = " ".join(str(command or "").strip().lower().split())
    if not lowered:
        return False
    if _is_cd_prefixed_long_running_command(lowered):
        return True
    if contains_stateful_shell_operation(lowered):
        return False
    return _is_direct_long_running_command(lowered)


def _is_cd_prefixed_long_running_command(command: str) -> bool:
    match = re.match(r"^cd\s+[^;&|]+&&\s*(?P<inner>.+)$", command)
    return bool(match and _is_direct_long_running_command(match.group("inner").strip()))


def contains_stateful_shell_operation(command: str) -> bool:
    patterns = (
        r"(?:^|[;&|]\s*)cd(?:\s|$)",
        r"\bset-location\b",
        r"(?:^|[;&|]\s*)export\s+",
        r"(?:^|[;&|]\s*)source\s+",
        r"(?:^|[;&|]\s*)set\s+",
        r"(?:^|[;&|]\s*)alias\s+",
        r"\bactivate\b",
        r"\bconda\s+activate\b",
    )
    return any(re.search(pattern, command) for pattern in patterns)


def _contains_stateful_shell_operation(command: str) -> bool:
    return contains_stateful_shell_operation(command)


def classify_safe_shell_command(command: str) -> SafeShellCommandKind:
    """Classify shell commands allowed in read-only contexts.

    This intentionally recognizes only simple commands and pipelines. Anything
    with shell redirection, compound control flow, mutation commands, or syntax
    we cannot cheaply reason about stays unsafe.
    """
    lowered = _normalize_command(command)
    if not lowered:
        return "unsafe"
    if contains_stateful_shell_operation(lowered):
        return "unsafe"
    if _has_unsafe_shell_syntax(lowered):
        return "unsafe"

    segments = _split_pipeline(lowered)
    if not segments:
        return "unsafe"

    segment_kinds = [_classify_pipeline_segment(segment) for segment in segments]
    if any(kind == "unsafe" for kind in segment_kinds):
        return "unsafe"
    if any(kind == "verify" for kind in segment_kinds[1:]):
        return "unsafe"
    if segment_kinds[0] == "verify":
        return "verify"
    return "read"


def _normalize_command(command: str) -> str:
    return " ".join(str(command or "").strip().lower().split())


def _classify_pipeline_segment(segment: str) -> SafeShellCommandKind:
    if not segment or _starts_with_assignment(segment):
        return "unsafe"
    if _has_write_output_option(segment):
        return "unsafe"
    if _matches_command_prefix(segment, _WRITE_PIPELINE_COMMAND_PREFIXES):
        return "unsafe"
    if _matches_command_prefix(segment, _VERIFY_COMMAND_PREFIXES):
        return "verify"
    if _matches_command_prefix(segment, _READ_COMMAND_PREFIXES):
        return "read"
    return "unsafe"


def _matches_command_prefix(command: str, prefixes: tuple[str, ...]) -> bool:
    return any(command == prefix or command.startswith(prefix + " ") for prefix in prefixes)


def _starts_with_assignment(command: str) -> bool:
    return bool(re.match(r"^[a-z_][a-z0-9_]*=", command))


def _has_write_output_option(command: str) -> bool:
    patterns = (
        r"^git\s+(diff|show)\b.*\s--output(?:=|\s+\S)",
        r"^sort\b.*\s-o\s+\S",
        r"^(pytest|python\s+-m\s+pytest)\b.*\s--junitxml(?:=|\s+\S)",
    )
    return any(re.search(pattern, command) for pattern in patterns)


def _has_unsafe_shell_syntax(command: str) -> bool:
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
        elif char == "`":
            return True
        elif char == "$" and index + 1 < len(command) and command[index + 1] == "(":
            return True
        elif char in {";", "&", "<", ">"}:
            return True
        index += 1
    return quote is not None


def _split_pipeline(command: str) -> list[str]:
    segments: list[str] = []
    current: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(command):
        char = command[index]
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            elif char == "\\" and quote == '"' and index + 1 < len(command):
                index += 1
                current.append(command[index])
            index += 1
            continue

        if char in {"'", '"'}:
            quote = char
            current.append(char)
        elif char == "|":
            next_char = command[index + 1] if index + 1 < len(command) else ""
            if next_char in {"|", "&"}:
                return []
            segment = "".join(current).strip()
            if not segment:
                return []
            segments.append(segment)
            current = []
        else:
            current.append(char)
        index += 1

    if quote is not None:
        return []
    segment = "".join(current).strip()
    if not segment:
        return []
    segments.append(segment)
    return segments


def _is_direct_long_running_command(command: str) -> bool:
    if _is_obviously_not_long_running(command):
        return False
    patterns = (
        r"^npm\s+run\s+(dev|start)(?:\s|$)",
        r"^npm\s+start(?:\s|$)",
        r"^(pnpm|yarn|bun)\s+(dev|start)(?:\s|$)",
        r"^(pnpm|yarn|bun)\s+run\s+(dev|start)(?:\s|$)",
        r"^vite(?:\s|$)",
        r"^npx\s+vite(?:\s|$)",
        r"^next\s+(dev|start)(?:\s|$)",
        r"^npx\s+next\s+(dev|start)(?:\s|$)",
        r"^webpack\s+serve(?:\s|$)",
        r"^npx\s+webpack\s+serve(?:\s|$)",
        r"^python\s+manage\.py\s+runserver(?:\s|$)",
        r"^python3\s+manage\.py\s+runserver(?:\s|$)",
        r"^flask\s+run(?:\s|$)",
        r"^python\s+-m\s+flask\s+run(?:\s|$)",
        r"^uvicorn\s+[\w.: -]+",
        r"^python\s+-m\s+uvicorn\s+[\w.: -]+",
        r"^fastapi\s+(dev|run)(?:\s|$)",
        r"^python\s+-m\s+http\.server(?:\s|$)",
        r"^python3\s+-m\s+http\.server(?:\s|$)",
        r"^tsc\b.*\s--watch(?:\s|$)",
        r"^cargo\s+watch(?:\s|$)",
    )
    return any(re.search(pattern, command) for pattern in patterns)


def _is_obviously_not_long_running(command: str) -> bool:
    blocked_fragments = (
        "npm test",
        "npm run test",
        "pnpm test",
        "pnpm run test",
        "yarn test",
        "yarn run test",
        "bun test",
        "pytest",
        "python -m pytest",
        "python -m unittest",
        "go test",
        "cargo test",
        "npm install",
        "pnpm install",
        "yarn install",
        "bun install",
        "pip install",
        "ruff format",
        "black ",
        "prettier ",
        "git ",
    )
    if command in {"python script.py", "node script.js"}:
        return True
    return any(command == item or command.startswith(item + " ") for item in blocked_fragments)
