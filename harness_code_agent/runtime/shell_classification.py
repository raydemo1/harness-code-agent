from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal


SafeShellCommandKind = Literal["read", "verify", "unsafe"]


@dataclass(frozen=True)
class ShellAnalysis:
    """One-pass shell command analysis shared by executor, policy and middlewares."""

    command: str
    risk: str                     # "shell_safe" | "shell_risky" | "shell_blocked"
    long_running: bool
    safe_kind: SafeShellCommandKind


_BLOCKED_PATTERNS = (
    r"\brm\s+-[^\n;|&]*[rf][^\n;|&]*(?:\s+--[^\n;|&]+)*\s+(?:/|/\*|~|~/\*|\.|\./\*|\*)\s*$",
    r"\bremove-item\b(?=.*-recurse\b)(?=.*(?:\bc:\\(?:\s|$)|\$home\b|~|(?:^|\s)\.(?:\s|$)|(?:^|\s)\*))",
    r"\bdel\b(?=.*(?:/[^\s]*s|-recurse\b))(?=.*(?:\bc:\\\*|\$home\b|~|(?:^|\s)\*))",
    r"\bmkfs(?:\.[\w-]+)?\b",
    r"(?:^|[;&|]\s*)format(?:\.com)?(?:\s|$)",
    r"\bdiskpart\b",
    r"\bdd\b.*\bof=/dev/",
)


@lru_cache(maxsize=1024)
def analyze_shell_command(command: str) -> ShellAnalysis:
    """Analyze a command once; every consumer shares this cached result."""
    lowered = str(command or "").strip().lower()
    if not lowered:
        return ShellAnalysis(lowered, "shell_safe", False, "unsafe")
    blocked = any(re.search(pattern, lowered) for pattern in _BLOCKED_PATTERNS) or all(
        fragment in lowered for fragment in (":(){", ":|:&", "};:")
    )
    safe_kind = classify_safe_shell_command(lowered)
    risk = "shell_blocked" if blocked else ("shell_safe" if safe_kind in {"read", "verify"} else "shell_risky")
    return ShellAnalysis(
        command=lowered,
        risk=risk,
        long_running=is_long_running_shell_command(lowered),
        safe_kind=safe_kind,
    )


_READ_COMMAND_PREFIXES = (
    "cat",
    "type",
    "ls",
    "dir",
    "pwd",
    "whoami",
    "id",
    "uname",
    "grep",
    "rg",
    "head",
    "tail",
    "curl -i",
    "curl -I",
    "curl --head",
    "echo",
    "git status",
    "git diff",
    "git log",
    "git show",
    "git branch",
    "git rev-parse",
    "git --version",
    "test",
    "diff",
    "wc",
    "md5sum",
    "sha1sum",
    "sha256sum",
    "shasum",
    "which",
    "where",
    "python --version",
    "python3 --version",
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
    "pdflatex",
    "latexmk",
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

    This intentionally recognizes simple read/verify commands, pipelines, and
    compound probes made from read-only commands. Anything with write
    redirection, mutation commands, command substitution, or syntax we cannot
    cheaply reason about stays unsafe.
    """
    lowered = _normalize_command(command)
    if not lowered:
        return "unsafe"
    lowered = _strip_scoped_cd_prefix(lowered)
    if contains_stateful_shell_operation(lowered):
        return "unsafe"
    if _has_unsafe_shell_syntax(lowered):
        return "unsafe"

    commands = _split_compound_commands(lowered)
    if not commands:
        return "unsafe"

    command_kinds = [_classify_simple_or_pipeline(command) for command in commands]
    if any(kind == "unsafe" for kind in command_kinds):
        return "unsafe"
    if any(kind == "verify" for kind in command_kinds):
        return "verify"
    return "read"


def _normalize_command(command: str) -> str:
    return " ".join(str(command or "").strip().lower().split())


def _strip_scoped_cd_prefix(command: str) -> str:
    match = re.match(r"^cd\s+[^;&|<>`$]+\s+&&\s*(?P<rest>.+)$", command)
    if match:
        return match.group("rest").strip()
    return command


def _classify_pipeline_segment(segment: str) -> SafeShellCommandKind:
    segment = _strip_safe_redirections(segment)
    segment = _normalize_git_global_options(segment)
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


def _normalize_git_global_options(command: str) -> str:
    match = re.match(r"^git\s+-c\s+\S+\s+(?P<rest>.+)$", command, flags=re.IGNORECASE)
    if match:
        return "git " + match.group("rest").strip()
    match = re.match(r"^git\s+-C\s+\S+\s+(?P<rest>.+)$", command, flags=re.IGNORECASE)
    if match:
        return "git " + match.group("rest").strip()
    return command


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
        elif char == ";":
            pass
        elif char == "&":
            next_char = command[index + 1] if index + 1 < len(command) else ""
            if next_char != "&":
                return True
            index += 1
        elif char == "|":
            next_char = command[index + 1] if index + 1 < len(command) else ""
            if next_char == "|":
                index += 1
        elif char == "<":
            return True
        elif char == ">":
            if not _redirection_at_is_safe(command, index):
                return True
            index = _redirection_end_index(command, index)
        index += 1
    return quote is not None


def _classify_simple_or_pipeline(command: str) -> SafeShellCommandKind:
    segments = _split_pipeline(command)
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


def _split_compound_commands(command: str) -> list[str]:
    parts: list[str] = []
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
        elif char == ";":
            part = "".join(current).strip()
            if not part:
                return []
            parts.append(part)
            current = []
        elif char in {"&", "|"} and index + 1 < len(command) and command[index + 1] == char:
            part = "".join(current).strip()
            if not part:
                return []
            parts.append(part)
            current = []
            index += 1
        else:
            current.append(char)
        index += 1

    if quote is not None:
        return []
    part = "".join(current).strip()
    if not part:
        return []
    parts.append(part)
    return parts


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


def _strip_safe_redirections(segment: str) -> str:
    previous = None
    current = segment.strip()
    while previous != current:
        previous = current
        current = re.sub(r"\s+\d?>\s*/dev/null(?:\s|$)", " ", current).strip()
        current = re.sub(r"\s+\d?>&\d(?:\s|$)", " ", current).strip()
    return current


def _redirection_at_is_safe(command: str, index: int) -> bool:
    prefix_start = index
    if index > 0 and command[index - 1].isdigit():
        prefix_start = index - 1
    operator = command[prefix_start : index + 1]
    remainder = command[index + 1 :].lstrip()
    if remainder.startswith("&"):
        return bool(re.match(r"&\d(?:\s|[;&|]|$)", remainder)) and operator in {"1>", "2>", ">"}
    return bool(re.match(r"/dev/null(?:\s|[;&|]|$)", remainder))


def _redirection_end_index(command: str, index: int) -> int:
    remainder = command[index + 1 :].lstrip()
    skipped_spaces = len(command[index + 1 :]) - len(remainder)
    match = re.match(r"(?:&\d|/dev/null)", remainder)
    if not match:
        return index
    return index + skipped_spaces + match.end()


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
