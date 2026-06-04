from __future__ import annotations

import re


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
