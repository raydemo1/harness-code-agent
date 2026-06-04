"""Git helpers for interactive sessions and checkpoints."""
from __future__ import annotations

import subprocess
from pathlib import Path

from .. import config
from ..sessions._event_helpers import is_ignored_changed_file


GIT_COMMIT_AUTHOR = ("Harness", "harness@example.invalid")
CHECKPOINT_EXCLUDES = [".harness", "global_plan", config.PROGRESS_FILE]


def _ensure_git_repository(workspace: Path) -> None:
    if (workspace / ".git").exists():
        _git_add_runtime_exclude(workspace)
        return
    subprocess.run(["git", "init"], cwd=workspace, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    _git_add_runtime_exclude(workspace)
    subprocess.run(git_commit_command("init", allow_empty=True), cwd=workspace, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)


def _git_add_runtime_exclude(workspace: Path) -> None:
    info_exclude = workspace / ".git" / "info" / "exclude"
    info_exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = info_exclude.read_text(encoding="utf-8", errors="replace") if info_exclude.exists() else ""
    lines = set(existing.splitlines())
    additions = [item for item in [".harness/", "global_plan/", config.PROGRESS_FILE] if item not in lines]
    if additions:
        with info_exclude.open("a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            for item in additions:
                f.write(item + "\n")


def git_add_runtime_excluded(workspace: Path) -> None:
    subprocess.run(
        ["git", "add", "-A", "--", "."],
        cwd=workspace,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )
    subprocess.run(
        ["git", "reset", "-q", "--", ".harness", "global_plan", config.PROGRESS_FILE],
        cwd=workspace,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def git_add_paths(workspace: Path, paths: list[str]) -> None:
    subprocess.run(
        ["git", "add", "--", *paths],
        cwd=workspace,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )


def git_has_committable_changes(workspace: Path) -> bool:
    result = subprocess.run(
        runtime_excluded_git_command("status", "--porcelain"),
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
    )
    return bool(result.stdout.strip())


def git_dirty_paths(workspace: Path) -> set[str]:
    result = subprocess.run(
        runtime_excluded_git_command("status", "--porcelain"),
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
    )
    paths: set[str] = set()
    for line in result.stdout.splitlines():
        if not line:
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[1]
        if path and not is_ignored_changed_file(path):
            paths.add(path)
    return paths


def git_staged_paths(workspace: Path) -> set[str]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
    )
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def git_has_staged_changes(workspace: Path) -> bool:
    result = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=workspace,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 1


def runtime_excluded_git_command(*args: str) -> list[str]:
    command = ["git", *args, "--", "."]
    command.extend(f":(exclude){item}" for item in CHECKPOINT_EXCLUDES)
    return command


def git_commit_command(message: str, *, allow_empty: bool = False) -> list[str]:
    name, email = GIT_COMMIT_AUTHOR
    command = [
        "git",
        "-c",
        f"user.name={name}",
        "-c",
        f"user.email={email}",
        "commit",
        "-m",
        message,
    ]
    if allow_empty:
        command.append("--allow-empty")
    return command
