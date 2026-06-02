from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..runtime.permissions import PermissionPolicy
from ..runtime.tool_result import ToolResult


FRESH_DETAIL_LIMIT = 12_000
STALE_REPLACEMENT_MIN_CHARS = 4_000
_OBS_ID_PREFIX = "[OBS "


@dataclass
class ToolObservation:
    id: str
    tool: str
    args_summary: str
    raw_output_path: Path
    summary: str
    resource_keys: list[str]
    observed_file_generations: dict[str, int]
    observed_workspace_generation: int
    output_chars: int
    output_hash: str
    created_at: float = field(default_factory=time.time)
    message_index: int | None = None
    stale: bool = False


class FactTracker:
    def __init__(self) -> None:
        self.file_generation: dict[str, int] = {}
        self.workspace_generation = 0
        self.invalidation_notices: list[str] = []
        self._policy = PermissionPolicy()

    def resource_keys_for(self, tool: str, args: dict[str, Any]) -> list[str]:
        if tool in {"read_file", "write_file", "apply_patch"} and args.get("path"):
            return [f"file:{_norm_path(args['path'])}"]
        if tool == "run_bash":
            return ["workspace"]
        return []

    def observed_file_generations(self, resource_keys: list[str]) -> dict[str, int]:
        result: dict[str, int] = {}
        for key in resource_keys:
            if key.startswith("file:"):
                path = key.removeprefix("file:")
                result[path] = self.file_generation.get(path, 0)
        return result

    def apply_mutation(
        self,
        *,
        tool: str,
        args: dict[str, Any],
        result: ToolResult,
        observations: list[ToolObservation],
        exclude_ids: set[str] | None = None,
    ) -> str:
        if result.status != "success":
            return ""
        exclude_ids = exclude_ids or set()
        changed: list[str] = []
        if tool in {"write_file", "apply_patch"} and args.get("path"):
            path = _norm_path(args["path"])
            old = self.file_generation.get(path, 0)
            self.file_generation[path] = old + 1
            changed.append(f"{path}: file_generation {old} -> {old + 1}")
        elif tool == "run_bash" and self._shell_may_mutate(str(args.get("command", ""))):
            old = self.workspace_generation
            self.workspace_generation = old + 1
            changed.append(f"workspace_generation {old} -> {old + 1}")
        if not changed:
            return ""

        stale_ids: list[str] = []
        for obs in observations:
            if obs.id in exclude_ids or obs.stale:
                continue
            if self.is_stale(obs):
                obs.stale = True
                stale_ids.append(obs.id)
        notice = (
            "[FACT INVALIDATION]\n"
            + "\n".join(changed)
            + "\nStale observations: "
            + (", ".join(stale_ids) if stale_ids else "none")
            + "\nTreat stale observations only as historical notes. Re-read files or rerun commands before relying on current facts."
        )
        self.invalidation_notices.append(notice)
        del self.invalidation_notices[:-5]
        if not stale_ids:
            return ""
        return notice

    def is_stale(self, observation: ToolObservation) -> bool:
        if observation.observed_workspace_generation < self.workspace_generation:
            return True
        for path, generation in observation.observed_file_generations.items():
            if generation < self.file_generation.get(path, 0):
                return True
        return False

    def _shell_may_mutate(self, command: str) -> bool:
        return self._policy.classify_shell_command(command) != "shell_safe"


class ObservationStore:
    MAX_OBSERVATIONS = 300

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._counter = 0
        self.observations: list[ToolObservation] = []

    def create(
        self,
        *,
        tool: str,
        args: dict[str, Any],
        result: ToolResult,
        fact_tracker: FactTracker,
    ) -> ToolObservation:
        self._counter += 1
        obs_id = f"obs_{self._counter:04d}"
        output = result.to_text()
        output_hash = hashlib.sha256(output.encode("utf-8", errors="replace")).hexdigest()[:16]
        raw_path = self.root / f"{obs_id}.txt"
        raw_path.write_text(output, encoding="utf-8")
        resource_keys = fact_tracker.resource_keys_for(tool, args)
        observation = ToolObservation(
            id=obs_id,
            tool=tool,
            args_summary=_args_summary(args),
            raw_output_path=raw_path,
            summary=_summary_for(tool, args, result, output_hash),
            resource_keys=resource_keys,
            observed_file_generations=fact_tracker.observed_file_generations(resource_keys),
            observed_workspace_generation=fact_tracker.workspace_generation,
            output_chars=len(output),
            output_hash=output_hash,
        )
        self.observations.append(observation)
        self._cleanup_old()
        return observation

    def _cleanup_old(self) -> None:
        excess = len(self.observations) - self.MAX_OBSERVATIONS
        if excess <= 0:
            return
        for obs in self.observations[:excess]:
            try:
                obs.raw_output_path.unlink(missing_ok=True)
            except OSError:
                pass
        del self.observations[:excess]

    def observed_message(self, observation: ToolObservation, result: ToolResult) -> str:
        output = result.to_text()
        detail = output
        if len(detail) > FRESH_DETAIL_LIMIT:
            omitted = len(detail) - FRESH_DETAIL_LIMIT
            detail = detail[:FRESH_DETAIL_LIMIT] + f"\n\n[TRUNCATED observation detail: {omitted} chars omitted]"
        return (
            f"[OBS {observation.id} observed]\n"
            f"tool: {observation.tool}\n"
            f"args: {observation.args_summary}\n"
            f"output_chars: {observation.output_chars}\n"
            f"output_sha256: {observation.output_hash}\n"
            f"resource_keys: {', '.join(observation.resource_keys) or 'none'}\n"
            "observation: This is the tool result as observed when the tool ran.\n\n"
            + detail
        )

    def historical_message(self, observation: ToolObservation) -> str:
        stale = "stale" if observation.stale else "historical"
        return (
            f"[OBS {observation.id} {stale}]\n"
            f"tool: {observation.tool}\n"
            f"args: {observation.args_summary}\n"
            f"output_chars: {observation.output_chars}\n"
            f"output_sha256: {observation.output_hash}\n"
            f"resource_keys: {', '.join(observation.resource_keys) or 'none'}\n"
            f"observed_workspace_generation: {observation.observed_workspace_generation}\n"
            f"summary: {observation.summary}\n"
            "current truth rule: This is a historical observation, not current truth. Re-read files or rerun commands before relying on exact/current facts."
        )

    def replace_long_stale_messages(
        self,
        messages: list[dict],
        *,
        min_chars: int = STALE_REPLACEMENT_MIN_CHARS,
        protect_from_index: int | None = None,
    ) -> list[str]:
        """Replace only long stale observation messages with compact summaries."""
        replaced: list[str] = []
        for observation in self.observations:
            if not observation.stale or observation.message_index is None:
                continue
            if protect_from_index is not None and observation.message_index >= protect_from_index:
                continue
            if observation.message_index >= len(messages):
                continue
            content = messages[observation.message_index].get("content") or ""
            if len(content) < min_chars:
                continue
            messages[observation.message_index]["content"] = self.historical_message(observation)
            replaced.append(observation.id)
        return replaced

    def detach_message_indexes(self, messages: list[dict]) -> None:
        """Drop durable message indexes after context replacement.

        Compaction/reset can reorder or replace messages, so stored indexes are
        no longer trustworthy. Any observation messages that survived in the
        new list are rewritten to historical summaries before indexes are
        cleared, preventing stale raw detail from lingering indefinitely.
        """
        observations_by_id = {observation.id: observation for observation in self.observations}
        for message in messages:
            if message.get("role") != "tool":
                continue
            content = message.get("content") or ""
            obs_id = _observation_id_from_header(content)
            if obs_id is None:
                continue
            observation = observations_by_id.get(obs_id)
            if observation is None:
                continue
            message["content"] = self.historical_message(observation)
        for observation in self.observations:
            observation.message_index = None


def _args_summary(args: dict[str, Any]) -> str:
    redacted = dict(args or {})
    if "content" in redacted:
        redacted["content"] = f"[{len(str(redacted['content']))} chars]"
    return json.dumps(redacted, ensure_ascii=False, sort_keys=True)


def _summary_for(tool: str, args: dict[str, Any], result: ToolResult, output_hash: str) -> str:
    if tool == "read_file":
        return f"read_file observed path={args.get('path', '')!s}; status={result.status}; output hash={output_hash}."
    if tool == "run_bash":
        return f"run_bash observed command={args.get('command', '')!s}; status={result.status}; return_code={result.return_code}."
    if tool in {"write_file", "apply_patch"}:
        return f"{tool} changed path={args.get('path', '')!s}; status={result.status}."
    return f"{tool} returned status={result.status}; output hash={output_hash}."


def _norm_path(path: object) -> str:
    return str(path).replace("\\", "/").strip()


def _indent(text: str, first_prefix: str) -> str:
    lines = text.splitlines() or [""]
    if len(lines) == 1:
        return first_prefix + lines[0]
    continuation = " " * len(first_prefix)
    return "\n".join(
        (first_prefix if idx == 0 else continuation) + line
        for idx, line in enumerate(lines)
    )


def _observation_id_from_header(content: str) -> str | None:
    parsed = _observation_header(content)
    return parsed[0] if parsed is not None else None


def _observation_header(content: str) -> tuple[str, str] | None:
    if not content.startswith(_OBS_ID_PREFIX):
        return None
    header_end = content.find("]", 5)
    if header_end < 0:
        return None
    header = content[5:header_end].strip()
    parts = header.split()
    if len(parts) < 2:
        return None
    return parts[0], parts[1]
