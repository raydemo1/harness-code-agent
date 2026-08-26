from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from functools import lru_cache

_PYTHON_COMMANDS = {"python", "python3", "python.exe", "python3.exe", "py", "py.exe"}


@dataclass(frozen=True)
class ShellAnalysis:
    """One-pass shell command analysis shared by executor, policy and middlewares."""

    command: str
    risk: str  # "shell_safe" | "shell_risky" | "shell_write_blocked" | "shell_blocked"
    long_running: bool
    kind: str  # inspect | verify | workspace_mutation | external_mutation | long_running | unknown | blocked
    approval_prefix: tuple[str, ...] | None = None
    sensitive_read: bool = False


_BLOCKED_PATTERNS = (
    r"\brm\s+-[^\n;|&]*[rf][^\n;|&]*(?:\s+--[^\n;|&]+)*\s+(?:/|/\*|~|~/\*|\.|\./\*|\*)\s*$",
    r"\bremove-item\b(?=.*-recurse\b)(?=.*(?:\bc:\\(?:\s|$)|\$home\b|~|(?:^|\s)\.(?:\s|$)|(?:^|\s)\*))",
    r"\bdel\b(?=.*(?:/[^\s]*s|-recurse\b))(?=.*(?:\bc:\\\*|\$home\b|~|(?:^|\s)\*))",
    r"(?:^|[;&|]\s*)(?:rmdir|rd)(?:\.exe)?\s+(?:/[^\s]+\s+)*(?:c:\\(?:\s|$)|c:\\\*|c:\\(?:windows|users|program files|programdata)(?:\\|\s|$)|\$home\b|~(?:\s|$)|\*)(?:\s|$)",
    r"(?:^|[;&|]\s*)git\s+(?:clean\b|reset\s+--hard\b|restore\b|checkout\s+--(?:\s|$)|push\b[^;&|]*--force(?:-with-lease)?\b)",
    r"\bmkfs(?:\.[\w-]+)?\b",
    r"(?:^|[;&|]\s*)format(?:\.com)?(?:\s|$)",
    r"\bdiskpart\b",
    r"\bdd\b.*\bof=/dev/",
    r"\bcipher\s+/w\b",
    r"\bbcdedit\b",
)

_SIDE_EFFECT_PATTERNS = (
    r"(?:^|[;&|]\s*)(?:mkdir|md|touch|install|npm\s+(?:install|i|ci|add|remove|uninstall|update|up|link|publish)|(?:pnpm|yarn|bun)\s+(?:install|i|add|remove|uninstall|update|up|link|publish)|(?:pip|pip3)\s+(?:install|uninstall)|(?:python(?:3)?|py)\s+-m\s+pip\s+(?:install|uninstall)|(?:cargo|go)\s+(?:add|get)|git\s+(?:add|init|apply|am|branch\s+-d|clean|reset|restore|commit|merge|rebase|cherry-pick|stash|push|fetch|revert|checkout\s+--|worktree\s+(?:add|remove)))(?:\s|$)",
    r"(?:^|[;&|]\s*)(?:python(?:3)?|py|node|deno|bun|pwsh|powershell|bash|sh|cmd)(?:\.exe)?\s+(?:-c|-e|--eval|/c|/k|-command|--command|-encodedcommand|-file)(?:\s|$)",
    r"(?:^|[;&|]\s*)(?:python(?:3)?|py|node|deno|bun|pwsh|powershell|bash|sh|cmd)(?:\.exe)?\s+[^;&|\s]+\.(?:py|js|ts|ps1|sh|bash|bat|cmd)(?:\s|$)",
    r"(?:^|[;&|]\s*)(?:eval|exec|invoke-expression|iex)(?:\s|$)",
    r"(?:^|[;&|]\s*)(?:fsutil|shutdown|restart-computer|reboot|poweroff|taskkill|stop-process|kill|pkill|stop-service|restart-service)(?:\s|$)",
    r"(?:^|[;&|]\s*)(?:icacls|takeown|set-acl|set-executionpolicy|chmod|chown|set-itemproperty|new-itemproperty|remove-itemproperty)(?:\s|$)",
    r"(?:^|[;&|]\s*)(?:reg\s+(?:add|delete)|schtasks\s+/(?:create|delete|run)|sc\s+(?:create|delete|stop|config))(?:\s|$)",
    r"\b(?:curl|wget)\b.*(?:\s(?:-x|--request)\s*(?:post|put|patch|delete)\b|\s(?:-d|--data(?:-raw|-binary)?|-f|--form|-t|--upload-file|-o|--output|--output-document|--remote-name|--remote-header-name|--create-dirs)\b)",
    r"\b(?:invoke-webrequest|invoke-restmethod)\b.*(?:-method\s+(?:post|put|patch|delete)\b|-body\b|-form\b|-infile\b|-outfile\b)",
    r"(?:^|[;&|]\s*)(?:ruff\s+format|black|gofmt\s+-w|cargo\s+fmt|prettier\b.*(?:--write|-w\b)|new-item)(?:\s|$)",
)

_DIRECT_FILE_MUTATION_PATTERNS = (
    r"(?:^|[;&|]\s*)(?:rm|rmdir|rd|del|erase|remove-item|clear-content|clear-item|clear-variable)(?:\.exe)?(?:\s|$)",
    r"(?:^|[;&|]\s*)(?:set-content|add-content|out-file|tee|tee-object|copy-item|move-item|rename-item|export-csv|export-clixml|set-acl|set-itemproperty|new-itemproperty|remove-itemproperty)(?:\s|$)",
    r"(?:^|[;&|]\s*)(?:cp|copy|copy-item|mv|move|move-item|rename|ren|ln|xcopy|robocopy|install|truncate|mktemp)(?:\s|$)",
    r"(?:^|[;&|]\s*)(?:sed|perl)\s+(?:(?:-[^\s;&|]+)\s+)*(?:-i|--in-place|-pi)(?:\s|$)",
    r"(?:^|[;&|]\s*)awk\s+(?:(?:-[^\s;&|]+)\s+)*-i\s+inplace(?:\s|$)",
    r"\b(?:curl|wget)\b.*\s(?:-o|--output|--output-document|--remote-name|--remote-header-name|--create-dirs)\b",
    r"\b(?:invoke-webrequest|invoke-restmethod)\b.*(?:-outfile\b|-out-file\b)",
)

_SHARED_WORKSPACE_STATE_PATTERNS = (
    r"(?:^|[;&|]\s*)git\s+(?:add|apply|am|commit|merge|rebase|cherry-pick|stash|branch\s+-d|worktree\s+(?:add|remove))(?:\s|$)",
    r"(?:^|[;&|]\s*)(?:mkdir|md|touch)(?:\s|$)",
    r"(?:^|[;&|]\s*)(?:npm|pnpm|yarn|bun)\s+(?:install|i|ci|add|remove|uninstall|update|up|link|publish)(?:\s|$)",
    r"(?:^|[;&|]\s*)(?:pip|pip3)\s+(?:install|uninstall)(?:\s|$)",
)

# This is deliberately narrower than the global shell policy. It is only the
# proof boundary for concurrent execution: commands that are not recognizably
# read/verify operations are routed to the serial lane instead of being
# treated as safe merely because no write pattern was found.
_PARALLEL_READ_PREFIXES = (
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
    "py --version",
    "node --version",
    "get-content",
    "gc",
    "get-childitem",
    "gci",
    "get-item",
    "get-location",
    "get-command",
    "gcm",
    "get-process",
    "gps",
    "get-service",
    "test-path",
    "resolve-path",
    "select-string",
    "sls",
    "where-object",
    "select-object",
    "get-member",
    "gm",
    "sort-object",
    "sort",
    "measure-object",
    "measure",
    "format-table",
    "ft",
    "write-output",
    "out-string",
    "convertfrom-json",
    "convertto-json",
    "split-path",
    "join-path",
    "compare-object",
    "compare",
    "cut",
    "uniq",
    "tr",
    "basename",
    "dirname",
    "realpath",
    "readlink",
    "file",
    "stat",
    "jq",
)

_PARALLEL_VERIFY_PREFIXES = (
    "python -m unittest",
    "python -m pytest",
    "pytest",
    "ruff check",
    "mypy",
    "npm test",
    "npm run test",
    "npm run build",
    "npm run lint",
    "npm run check",
    "npm run typecheck",
    "pnpm test",
    "pnpm run test",
    "pnpm run build",
    "pnpm run lint",
    "pnpm run check",
    "pnpm run typecheck",
    "yarn test",
    "yarn run test",
    "yarn run build",
    "yarn run lint",
    "yarn run check",
    "yarn run typecheck",
    "bun test",
    "bun run test",
    "bun run build",
    "bun run lint",
    "bun run check",
    "bun run typecheck",
    "go test",
    "cargo test",
    "tsc --noemit",
    "pdflatex",
    "latexmk",
    "make test",
)


@lru_cache(maxsize=1024)
def analyze_shell_command(command: str) -> ShellAnalysis:
    """Classify only explicit danger and side effects; unknown commands stay usable."""
    lowered = " ".join(str(command or "").strip().lower().split())
    if not lowered:
        return ShellAnalysis(lowered, "shell_safe", False, "unknown")

    blocked = any(re.search(pattern, lowered) for pattern in _BLOCKED_PATTERNS) or all(
        fragment in lowered for fragment in (":(){", ":|:&", "};:")
    )
    direct_file_mutation = _has_direct_file_mutation(
        lowered
    ) or _has_file_write_redirection(lowered)
    direct_file_mutation = direct_file_mutation or _has_write_output_option(lowered)
    if blocked:
        risk = "shell_blocked"
    elif direct_file_mutation:
        risk = "shell_write_blocked"
    elif any(
        re.search(pattern, lowered) for pattern in _SIDE_EFFECT_PATTERNS
    ) or contains_stateful_shell_operation(lowered):
        risk = "shell_risky"
    else:
        risk = "shell_safe"
    long_running = is_long_running_shell_command(lowered)
    if risk in {"shell_blocked", "shell_write_blocked"}:
        kind = "blocked"
    elif long_running:
        kind = "long_running"
    elif risk == "shell_risky":
        kind = (
            "external_mutation"
            if _is_external_mutation(lowered)
            else "workspace_mutation"
        )
    elif is_verify_shell_command(lowered):
        kind = "verify"
    elif is_inspection_shell_command(lowered):
        kind = "inspect"
    else:
        kind = "unknown"
    prefix = derive_persistent_prefix(lowered)
    sensitive_read = bool(re.match(r"^(?:env|printenv)(?:\s|$)", lowered))
    return ShellAnalysis(
        lowered,
        risk,
        long_running,
        kind,
        tuple(prefix) if prefix else None,
        sensitive_read,
    )


def derive_persistent_prefix(command: str) -> list[str] | None:
    """Return one stable approval prefix for a simple command or compound."""
    segments = _split_simple_compound(command)
    if len(segments) > 1:
        prefixes = [
            prefix
            for segment in segments
            if not _is_literal_expression(segment)
            for prefix in [_derive_single_prefix(segment)]
        ]
        if prefixes and all(prefix == prefixes[0] for prefix in prefixes):
            return prefixes[0]
        return None
    return _derive_single_prefix(command)


def command_matches_prefix(command: str, prefix: list[str]) -> bool:
    """Match every meaningful segment against one normalized approval prefix."""
    segments = _split_simple_compound(command)
    if not segments:
        return False
    matched = False
    for segment in segments:
        if _is_literal_expression(segment):
            continue
        tokens = [
            _normalize_token(token) for token in _tokenize_approval_command(segment)
        ]
        if not tokens or tokens[: len(prefix)] != prefix:
            return False
        matched = True
    return matched


def _derive_single_prefix(command: str) -> list[str] | None:
    tokens = _tokenize_approval_command(command)
    if len(tokens) < 2:
        return None
    normalized = [_normalize_token(token) for token in tokens]
    python_index = next(
        (index for index, token in enumerate(normalized) if token in _PYTHON_COMMANDS),
        None,
    )
    if python_index is not None:
        if len(normalized) <= python_index + 1:
            return None
        launcher = normalized[python_index + 1]
        if launcher in {"-", "-c", "-i"}:
            return None
        if launcher == "-m":
            if len(normalized) <= python_index + 2:
                return None
            return normalized[: python_index + 3]
        if launcher.startswith("-"):
            return None
        return normalized[: python_index + 2]
    return normalized[: min(3, len(normalized))]


def _split_simple_compound(command: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    quote = ""
    text = command.strip()
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            current.append(char)
        elif char == ";" or (
            char == "&" and index + 1 < len(text) and text[index + 1] == "&"
        ):
            part = "".join(current).strip()
            if part:
                parts.append(part)
            current = []
            if char == "&":
                index += 1
        elif char in "|&<>":
            return []
        else:
            current.append(char)
        index += 1
    if quote:
        return []
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def _tokenize_approval_command(command: str) -> list[str]:
    if not command.strip() or re.search(r"[|;&<>]", command):
        return []
    try:
        return shlex.split(command, posix=False)
    except ValueError:
        return []


def _is_literal_expression(command: str) -> bool:
    text = command.strip()
    return len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}


def _normalize_token(token: object) -> str:
    return str(token).strip().strip("\"'").lower()


def is_verify_shell_command(command: str) -> bool:
    normalized = _normalize_command(command)
    return any(
        normalized == prefix or normalized.startswith(prefix + " ")
        for prefix in _PARALLEL_VERIFY_PREFIXES
    )


def is_inspection_shell_command(command: str) -> bool:
    normalized = _normalize_command(command)
    if contains_stateful_shell_operation(normalized) or any(
        token in normalized for token in (";", "&&", "||", "<", "`", "$(")
    ):
        return False
    segments = _split_parallel_pipeline(normalized)
    return bool(segments) and all(
        any(
            _strip_parallel_safe_redirections(segment) == prefix
            or _strip_parallel_safe_redirections(segment).startswith(prefix + " ")
            for prefix in _PARALLEL_READ_PREFIXES
        )
        for segment in segments
    )


def _is_external_mutation(command: str) -> bool:
    return bool(
        re.search(
            r"(?:npm|pnpm|yarn|bun|pip|cargo|go)\s+(?:install|add|remove|publish)|git\s+(?:push|fetch)|invoke-restmethod|curl\b.*(?:--data|-d|-x\s*(?:post|put|patch|delete))",
            command,
        )
    )


def is_workspace_write_shell_command(command: str) -> bool:
    """Detect direct workspace writes without maintaining a command allowlist."""
    normalized = _normalize_command(command)
    if not normalized:
        return False
    return (
        _has_direct_file_mutation(normalized)
        or _has_file_write_redirection(normalized)
        or _has_write_output_option(normalized)
        or any(
            re.search(pattern, normalized)
            for pattern in _SHARED_WORKSPACE_STATE_PATTERNS
        )
        or bool(
            re.search(
                r"(?:^|[;&|]\s*)(?:ruff\s+format|black|gofmt\s+-w|cargo\s+fmt|prettier\b.*(?:--write|-w\b)|new-item)(?:\s|$)",
                normalized,
            )
        )
    )


def is_parallel_shell_command_unsafe(command: str) -> bool:
    """Return whether a command must leave the concurrent read lane."""
    return classify_parallel_shell_command(command) != "parallel_read"


def classify_parallel_shell_command(command: str) -> str:
    """Classify a command for the batch executor.

    ``parallel_read`` is a proof of read/verify behavior. ``serial_write``
    includes normal side effects and unknown commands. ``blocked`` is reserved
    for the global hard safety boundary.
    """
    analysis = analyze_shell_command(command)
    if analysis.risk in {"shell_blocked", "shell_write_blocked"}:
        return "blocked"
    if is_parallel_shell_command_safe(command):
        return "parallel_read"
    return "serial_write"


def is_parallel_shell_command_safe(command: str) -> bool:
    """Prove that a shell command is safe to run concurrently.

    This is an execution-lane guard, not the global permission allowlist. A
    command that cannot be recognized here is intentionally sent to serial
    execution, even when the global classifier has not found a write pattern.
    """
    normalized = _normalize_command(command)
    if not normalized:
        return False
    analysis = analyze_shell_command(normalized)
    if analysis.risk in {"shell_blocked", "shell_write_blocked"}:
        return False
    if contains_stateful_shell_operation(normalized):
        return False
    if _has_file_write_redirection(normalized) or _has_write_output_option(normalized):
        return False
    if any(token in normalized for token in (";", "&&", "||", "<", "`", "$(")):
        return False
    segments = _split_parallel_pipeline(normalized)
    if not segments:
        return False
    return all(_is_parallel_read_segment(segment) for segment in segments)


def _is_parallel_read_segment(segment: str) -> bool:
    segment = _strip_parallel_safe_redirections(segment)
    if not segment:
        return False
    return any(
        segment == prefix or segment.startswith(prefix + " ")
        for prefix in (*_PARALLEL_READ_PREFIXES, *_PARALLEL_VERIFY_PREFIXES)
    )


def _split_parallel_pipeline(command: str) -> list[str]:
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
            if index + 1 < len(command) and command[index + 1] == "|":
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


def _strip_parallel_safe_redirections(segment: str) -> str:
    current = segment.strip()
    previous = None
    while current != previous:
        previous = current
        current = re.sub(
            r"\s+\d?>\s*(?:/dev/null|\$null)(?:\s|$)", " ", current
        ).strip()
        current = re.sub(r"\s+\d?>&\d(?:\s|$)", " ", current).strip()
    return current


def is_long_running_shell_command(command: str) -> bool:
    lowered = _normalize_command(command)
    if not lowered:
        return False
    if _is_cd_prefixed_long_running_command(lowered):
        return True
    if contains_stateful_shell_operation(lowered):
        return False
    return _is_direct_long_running_command(lowered)


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


def _normalize_command(command: str) -> str:
    return " ".join(str(command or "").strip().lower().split())


def _has_direct_file_mutation(command: str) -> bool:
    return any(
        re.search(pattern, command) for pattern in _DIRECT_FILE_MUTATION_PATTERNS
    )


def _has_write_output_option(command: str) -> bool:
    patterns = (
        r"^git\s+(diff|show)\b.*\s--output(?:=|\s+\S)",
        r"^sort\b.*\s-o\s+\S",
        r"^(pytest|python\s+-m\s+pytest)\b.*\s--junitxml(?:=|\s+\S)",
    )
    return any(re.search(pattern, command) for pattern in patterns)


def _has_file_write_redirection(command: str) -> bool:
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
        elif char == ">" and not _redirection_at_is_safe(command, index):
            return True
        index += 1
    return False


def _redirection_at_is_safe(command: str, index: int) -> bool:
    prefix_start = index
    if index > 0 and command[index - 1].isdigit():
        prefix_start = index - 1
    operator = command[prefix_start : index + 1]
    remainder = command[index + 1 :].lstrip()
    if remainder.startswith("&"):
        return bool(re.match(r"&\d(?:\s|[;&|]|$)", remainder)) and operator in {
            "1>",
            "2>",
            ">",
        }
    return bool(re.match(r"(?:/dev/null|\$null)(?:\s|[;&|]|$)", remainder))


def _is_cd_prefixed_long_running_command(command: str) -> bool:
    match = re.match(r"^cd\s+[^;&|]+&&\s*(?P<inner>.+)$", command)
    return bool(match and _is_direct_long_running_command(match.group("inner").strip()))


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
    return any(
        command == item or command.startswith(item + " ") for item in blocked_fragments
    )
