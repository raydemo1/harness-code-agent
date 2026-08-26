"""Loop detection middleware."""
from __future__ import annotations

import hashlib
import json
import logging

from ..arg_preview import safe_args_preview as _shared_safe_args_preview
from ..tool_result import ToolResult
from .base import AgentMiddleware

log = logging.getLogger("harness")


class LoopDetectionMiddleware(AgentMiddleware):
    """
    Tracks per-file edit counts and detects repetitive command patterns.
    When the agent edits the same file or runs similar commands too many times,
    injects a nudge to reconsider the approach.

    Uses fuzzy matching for commands — catches variants like:
      python3 app.py  /  python3 app.py 2>&1  /  python3 ./app.py
    """

    def __init__(
        self,
        file_edit_threshold: int = 4,
        command_repeat_threshold: int = 3,
        tool_fingerprint_repeat_threshold: int = 3,
    ):
        self.file_edit_threshold = file_edit_threshold
        self.command_repeat_threshold = command_repeat_threshold
        self.tool_fingerprint_repeat_threshold = tool_fingerprint_repeat_threshold
        self.file_edit_counts: dict[str, int] = {}
        self.recent_commands: list[str] = []
        self.recent_tool_fingerprints: list[tuple[str, str]] = []
        self.recent_tool_summaries: list[str] = []
        self._fingerprint_warned: set[tuple[str, str]] = set()
        self._file_warned: set[str] = set()  # avoid spamming same warning

    def begin_turn(self, task: str, messages: list[dict], runtime_state=None,
                   agent_name: str | None = None) -> None:
        self.file_edit_counts.clear()
        self.recent_commands.clear()
        self.recent_tool_fingerprints.clear()
        self.recent_tool_summaries.clear()
        self._fingerprint_warned.clear()
        self._file_warned.clear()

    @staticmethod
    def _normalize_command(cmd: str) -> str:
        """Normalize a command for fuzzy comparison."""
        import re
        cmd = cmd.strip()
        # Remove common suffixes that don't change semantics
        cmd = re.sub(r'\s*2>&1\s*$', '', cmd)
        cmd = re.sub(r'\s*\|\s*head.*$', '', cmd)
        cmd = re.sub(r'\s*\|\s*tail.*$', '', cmd)
        # Normalize paths: ./foo → foo
        cmd = re.sub(r'\./(\S)', r'\1', cmd)
        # Collapse whitespace
        cmd = re.sub(r'\s+', ' ', cmd)
        return cmd.strip()

    @staticmethod
    def _fingerprint(tool_name: str, tool_args: dict) -> tuple[str, str]:
        normalized = json.dumps(tool_args or {}, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
        return tool_name, digest

    @staticmethod
    def _safe_args_preview(tool_args: dict) -> str:
        """Preview for loop-detection traces (220-char limit, consistent with legacy behavior)."""
        return _shared_safe_args_preview(tool_args, max_chars=220)

    def _observe_tool_fingerprint(self, tool_name: str, tool_args: dict, runtime_state=None) -> str | None:
        threshold = self.tool_fingerprint_repeat_threshold
        if threshold <= 1:
            return None
        fingerprint = self._fingerprint(tool_name, tool_args)
        summary = f"{tool_name}({self._safe_args_preview(tool_args)})"
        self.recent_tool_fingerprints.append(fingerprint)
        self.recent_tool_summaries.append(summary)
        self.recent_tool_fingerprints = self.recent_tool_fingerprints[-threshold:]
        self.recent_tool_summaries = self.recent_tool_summaries[-5:]

        fallback = getattr(runtime_state, "fallback", None)
        if fallback is not None:
            fallback.record_action(summary)

        if len(self.recent_tool_fingerprints) < threshold:
            return None
        if len(set(self.recent_tool_fingerprints[-threshold:])) != 1:
            return None

        if fingerprint in self._fingerprint_warned:
            if fallback is not None:
                fallback.request_stop(
                    reason="loop_detected",
                    limit_type="tool_fingerprint",
                    used=threshold + 1,
                    limit=threshold,
                    last_tool=tool_name,
                    fingerprint_hash=fingerprint[1],
                    recent_action_summary=self.recent_tool_summaries,
                )
            return None

        self._fingerprint_warned.add(fingerprint)
        log.warning("Loop detection: identical tool fingerprint repeated %sx", threshold)
        return (
            f"[SYSTEM] You have made the same tool call {threshold} times in a row.\n"
            f"Tool call pattern: {summary}\n"
            "This looks like a loop. Do not repeat it again. Change strategy, summarize what you know, "
            "or ask for a decision if the task is blocked."
        )

    def post_tool(self, tool_name: str, tool_args: dict, result: ToolResult,
                  messages: list[dict], runtime_state=None,
                  agent_name: str | None = None) -> str | None:
        # Track file edits
        if tool_name == "write_file":
            path = tool_args.get("path", "")
            self.file_edit_counts[path] = self.file_edit_counts.get(path, 0) + 1
            count = self.file_edit_counts[path]
            if count >= self.file_edit_threshold and path not in self._file_warned:
                self._file_warned.add(path)
                log.warning(f"Loop detection: {path} edited {count} times")
                return (
                    f"[SYSTEM] You have edited '{path}' {count} times. "
                    "This pattern suggests your current approach may not be working. "
                    "STOP and reconsider:\n"
                    "1. Re-read the original task requirements.\n"
                    "2. Think about what's fundamentally wrong with your approach.\n"
                    "3. Try a completely different strategy."
                )

        # Track repeated commands (with fuzzy matching)
        if tool_name == "run_bash":
            cmd = tool_args.get("command", "").strip()
            self.recent_commands.append(cmd)
            if len(self.recent_commands) >= self.command_repeat_threshold:
                window = self.recent_commands[-self.command_repeat_threshold:]
                normalized = [self._normalize_command(c) for c in window]
                if len(set(normalized)) == 1:
                    log.warning(f"Loop detection: similar command repeated {self.command_repeat_threshold}x")
                    return (
                        f"[SYSTEM] You have run essentially the same command {self.command_repeat_threshold} "
                        f"times in a row with no progress.\n"
                        f"Command pattern: {normalized[0][:200]}\n"
                        "This is a doom loop. The same action will not produce a different result.\n"
                        "STOP. Re-read the error output carefully. Try a fundamentally different approach."
                    )

            # Also detect rapid-fire failed commands (different commands, same error)
            if result.status == "failed":
                recent_errors = 0
                for msg in reversed(messages[-8:]):
                    content = msg.get("content", "")
                    if msg.get("role") == "tool" and (
                        "[error]" in content or "command not found" in content.lower()
                    ):
                        recent_errors += 1
                if recent_errors >= 3:
                    return (
                        "[SYSTEM] Multiple consecutive commands have failed. "
                        "Stop and diagnose the root cause before trying more commands. "
                        "Check: Is the required tool installed? Are you in the right directory? "
                        "Is there a dependency missing?"
                    )

        fingerprint_warning = self._observe_tool_fingerprint(tool_name, tool_args, runtime_state)
        if fingerprint_warning:
            return fingerprint_warning

        return None
