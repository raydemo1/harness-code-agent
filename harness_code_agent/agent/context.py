"""Context lifecycle helpers for lightweight auto-compaction.

Auto-compaction starts near the context limit, first folds older tool outputs,
then summarizes older conversation only if necessary. It never rewrites the
system prompt or tools schema.
"""
from __future__ import annotations

import re
import hashlib
import json
import logging
from dataclasses import dataclass, field

from .. import config

log = logging.getLogger("harness")

DEFAULT_RECENT_TAIL_TOKENS = 16_384
DEFAULT_MIN_RECENT_MESSAGES = 2
MIN_COMPACT_REGION_TOKENS = 400

# ---------------------------------------------------------------------------
# Token counting
# ---------------------------------------------------------------------------

# Try tiktoken for accurate counting; fall back to char-based estimation.
# This removes tiktoken as a hard dependency — critical for TB2 environments
# where pip install may be slow or unavailable.
_encoder = None
_use_tiktoken = False

try:
    import tiktoken
    _use_tiktoken = True
except ImportError:
    pass


def _get_encoder():
    global _encoder
    if not _use_tiktoken:
        return None
    if _encoder is None:
        try:
            _encoder = tiktoken.encoding_for_model(config.MODEL)
        except Exception:
            _encoder = tiktoken.get_encoding("cl100k_base")
    return _encoder


def count_tokens(messages: list[dict]) -> int:
    """Rough token count for a message list.
    Uses tiktoken if available, otherwise estimates ~4 chars per token."""
    enc = _get_encoder()
    total = 0
    for msg in messages:
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = " ".join(
                block.get("text", "") for block in content if isinstance(block, dict)
            )
        text = str(content)
        if enc:
            total += len(enc.encode(text)) + 4
        else:
            # ~4 chars per token is a reasonable approximation
            total += len(text) // 4 + 4
        for tc in msg.get("tool_calls", []):
            args = str(tc.get("function", {}).get("arguments", ""))
            if enc:
                total += len(enc.encode(args))
            else:
                total += len(args) // 4
    return total


def count_request_tokens(messages: list[dict], *, tool_schemas: list[dict] | None = None) -> int:
    """Estimate the full request size, including tool schema overhead."""
    total = count_tokens(messages)
    if not tool_schemas:
        return total
    schema_text = json.dumps(tool_schemas, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    enc = _get_encoder()
    if enc:
        return total + len(enc.encode(schema_text))
    return total + len(schema_text) // 4


# ---------------------------------------------------------------------------
# Context anxiety detection
# ---------------------------------------------------------------------------

@dataclass
class ContextAnxietySignal:
    detected: bool = False
    score: int = 0
    reasons: list[str] = field(default_factory=list)
    source: str = "assistant_recent_messages"

    def __bool__(self) -> bool:
        return self.detected


# Patterns that indicate the model is trying to wrap up prematurely
_ANXIETY_PATTERNS = [
    r"(?i)let me wrap up",
    r"(?i)i('ll| will) finalize",
    r"(?i)that should be (enough|sufficient)",
    r"(?i)i('ll| will) stop here",
    r"(?i)due to (the )?(context |token )?limit",
    r"(?i)running (low on|out of) (context|space|tokens)",
    r"(?i)to (save|conserve) (context|space|tokens)",
    r"(?i)i('ve| have) covered the (main|key|essential)",
    r"(?i)in the interest of (time|space|brevity)",
    r"上下文.*(快满|快没|不够|用完|空间)",
    r"(快没|没有|没).*上下文",
    r"先收尾",
    r"只覆盖关键",
]


def detect_anxiety(messages: list[dict]) -> ContextAnxietySignal:
    """
    Check recent assistant messages for signs of context anxiety —
    the model trying to wrap up work prematurely because it thinks
    it's running out of context space.
    """
    # Only check the last few assistant messages
    recent_texts = []
    for msg in reversed(messages[-10:]):
        if msg.get("role") == "assistant" and msg.get("content"):
            recent_texts.append(msg["content"])
        if len(recent_texts) >= 3:
            break

    combined = " ".join(recent_texts)
    reasons: list[str] = []
    for pattern in _ANXIETY_PATTERNS:
        match = re.search(pattern, combined)
        if match:
            reasons.append(match.group(0))
    if len(reasons) >= 2:
        log.warning(f"Context anxiety detected ({len(reasons)} signals found)")
        return ContextAnxietySignal(
            detected=True,
            score=len(reasons),
            reasons=reasons,
        )
    return ContextAnxietySignal()


# ---------------------------------------------------------------------------
# Compaction
# ---------------------------------------------------------------------------

def compact_messages(
    messages: list[dict],
    llm_call,
    role: str = "default",
    *,
    force: bool = False,
    target_tokens: int | None = None,
    recent_tail_budget_tokens: int | None = None,
) -> list[dict]:
    """
    Summarize the older portion of messages, keep the system prompt
    and recent messages intact.

    llm_call: a callable(messages) -> str that calls the LLM for summarization.
    force: if True, can compress segments within the current user turn
           (but never splits tool_call/tool result pairs).
    """
    if not messages:
        return messages

    system = [messages[0]] if messages[0].get("role") == "system" else []
    non_system = messages[len(system):]
    split_idx = choose_compaction_split_index(
        messages,
        force=force,
        target_tokens=target_tokens,
        recent_tail_budget_tokens=recent_tail_budget_tokens,
    ) - len(system)
    old = non_system[:split_idx]
    recent = non_system[split_idx:]

    if not old:
        return messages
    if not force and not _compaction_economics(old):
        return messages

    old_text = _messages_to_text(old)

    summarize_instruction = (
        "Summarize the following agent work log. Preserve: key decisions, "
        "files created/modified, current progress, and errors encountered."
    )

    summary = llm_call([
        {"role": "system", "content": f"You are a concise summarizer. {summarize_instruction}"},
        {"role": "user", "content": old_text},
    ])

    summary_msg = {
        "role": "user",
        "content": f"[COMPACTED CONTEXT — summary of earlier work]\n{summary}",
    }

    return system + [summary_msg] + recent


def summarize_older_conversation(
    messages: list[dict],
    llm_call,
    *,
    current_turn_start_index: int,
) -> list[dict]:
    """Summarize conversation before the current turn and keep current turn raw."""
    if not messages:
        return messages

    system = [messages[0]] if messages[0].get("role") == "system" else []
    system_len = len(system)
    split = max(system_len, min(current_turn_start_index, len(messages)))
    older = messages[system_len:split]
    current = messages[split:]
    if not older:
        return messages
    if not _compaction_economics(older):
        return messages

    old_text = _messages_to_text(older)
    summary = llm_call([
        {"role": "system", "content": (
            "You are summarizing older conversation history for a coding agent. "
            "Preserve decisions, active constraints, changed files, files touched, "
            "failed commands, recent errors, current status, and next action. "
            "Discard old full tool output, repeated logs, and resolved branches."
        )},
        {"role": "user", "content": old_text},
    ])
    summary_msg = {
        "role": "user",
        "content": f"[COMPACTED CONTEXT — summary of older conversation]\n{summary}",
    }
    return system + [summary_msg] + current


def rebuild_working_context(
    messages: list[dict],
    state: dict,
    *,
    current_turn_start_index: int,
    max_turns: int = 5,
) -> list[dict]:
    """Rebuild a concise working context without carrying old raw payloads."""
    if not messages:
        return messages

    system = [messages[0]] if messages[0].get("role") == "system" else []
    recent = _last_turn_messages(messages, max_turns=max_turns)
    recent_text = _messages_to_text([
        _sanitize_message_for_rebuild(msg)
        for msg in recent
        if msg.get("role") != "system"
    ])
    content = (
        "[REBUILD_WORKING_CONTEXT]\n"
        "The previous context was rebuilt to avoid repeated auto-compaction. "
        "Use this as the active working state.\n\n"
        f"## Current User Task\n{_state_value(state, 'current_user_task')}\n\n"
        f"## Active Plan / Status\n{_state_value(state, 'active_plan_status')}\n\n"
        f"## Changed Files\n{_state_list(state, 'changed_files')}\n\n"
        f"## Files Touched\n{_state_list(state, 'files_touched')}\n\n"
        f"## Recent Errors\n{_state_list(state, 'recent_errors')}\n\n"
        f"## Failed Commands\n{_state_list(state, 'failed_commands')}\n\n"
        f"## Active Constraints\n{_state_list(state, 'active_constraints')}\n\n"
        f"## Latest Checkpoint Summary\n{_state_value(state, 'latest_checkpoint_summary')}\n\n"
        f"## Last {max_turns} Turns\n{recent_text or 'none'}\n\n"
        f"## Next Recommended Action\n{_state_value(state, 'next_recommended_action')}\n\n"
        "Discarded: old full tool output, old full read_file content, repeated logs, and resolved branches."
    )
    return system + [{"role": "user", "content": content}]


def choose_compaction_split_index(
    messages: list[dict],
    *,
    force: bool = False,
    target_tokens: int | None = None,
    recent_tail_budget_tokens: int | None = None,
) -> int:
    """Return an absolute split index for replacing the old prefix."""
    if not messages:
        return 0

    system = [messages[0]] if messages[0].get("role") == "system" else []
    non_system = messages[len(system):]
    tail_budget = _recent_tail_budget_tokens(
        recent_tail_budget_tokens=recent_tail_budget_tokens,
        target_tokens=target_tokens,
    )
    split_idx = _token_bounded_tail_start(non_system, tail_budget)
    if target_tokens is not None:
        placeholder_summary = {
            "role": "user",
            "content": "[COMPACTED CONTEXT — summary of earlier work]\n",
        }
        split_idx = _fit_tail_start_to_budget(
            system,
            placeholder_summary,
            non_system,
            split_idx,
            target_tokens,
        )
    return len(system) + split_idx


def _recent_tail_budget_tokens(
    *,
    recent_tail_budget_tokens: int | None,
    target_tokens: int | None,
) -> int:
    if recent_tail_budget_tokens is not None:
        try:
            return max(1, int(recent_tail_budget_tokens))
        except (TypeError, ValueError):
            return DEFAULT_RECENT_TAIL_TOKENS
    budget = DEFAULT_RECENT_TAIL_TOKENS
    if target_tokens is not None:
        try:
            budget = min(budget, max(1, int(target_tokens * 0.5)))
        except (TypeError, ValueError):
            pass
    else:
        budget = min(budget, max(1, int(config.CONTEXT_WINDOW_TOKENS * 0.5)))
    return budget


def _token_bounded_tail_start(
    messages: list[dict],
    budget_tokens: int,
    *,
    min_recent_messages: int = DEFAULT_MIN_RECENT_MESSAGES,
) -> int:
    if not messages:
        return 0
    start = len(messages)
    token_count = 0
    min_recent_messages = max(0, min_recent_messages)
    for idx in range(len(messages) - 1, -1, -1):
        msg_tokens = count_tokens([messages[idx]])
        kept = len(messages) - idx
        if kept > min_recent_messages and token_count + msg_tokens > budget_tokens:
            break
        token_count += msg_tokens
        start = idx
    return _safe_split_index(messages, start)


def _compaction_economics(messages: list[dict]) -> bool:
    return count_tokens(messages) >= MIN_COMPACT_REGION_TOKENS


def _safe_split_index(messages: list[dict], target_idx: int) -> int:
    """Find a safe split point that doesn't break tool_call/tool message pairs.

    Scans backward from target_idx to find a position where the message
    at target_idx is NOT a 'tool' response (which must stay with its
    preceding assistant message).
    """
    idx = max(0, min(target_idx, len(messages)))

    # Walk backward until we're not inside a tool_call/tool pair
    while idx > 0 and idx < len(messages):
        msg = messages[idx]
        if msg.get("role") == "tool":
            # This is a tool response — can't split here, move back
            idx -= 1
        elif msg.get("role") == "assistant" and msg.get("tool_calls"):
            # This assistant message has tool_calls — its tool responses
            # follow it, so we can't split here either. Move back.
            idx -= 1
        else:
            break

    return idx


def _safe_tail_start_at_or_after(messages: list[dict], target_idx: int) -> int:
    """Find a safe tail start at or after target_idx without orphaning tool results."""
    idx = max(0, min(target_idx, len(messages)))
    while idx < len(messages):
        msg = messages[idx]
        if msg.get("role") == "tool":
            idx += 1
            continue
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            idx += 1
            while idx < len(messages) and messages[idx].get("role") == "tool":
                idx += 1
            continue
        break
    return idx


def _fit_tail_start_to_budget(
    system: list[dict],
    summary_msg: dict,
    non_system: list[dict],
    split_idx: int,
    target_tokens: int,
) -> int:
    compacted = system + [summary_msg] + non_system[split_idx:]
    while count_tokens(compacted) > target_tokens and split_idx < len(non_system) - 1:
        next_idx = _safe_tail_start_at_or_after(non_system, split_idx + 1)
        if next_idx <= split_idx or next_idx >= len(non_system):
            break
        split_idx = next_idx
        compacted = system + [summary_msg] + non_system[split_idx:]
    return split_idx


# ---------------------------------------------------------------------------
# Manual checkpoint restore helpers
# ---------------------------------------------------------------------------

def create_checkpoint(messages: list[dict], llm_call) -> str:
    """
    Serialize current state into a structured handoff document.
    Persists to progress.md so it survives across sessions.
    Returns the checkpoint text.
    """
    from pathlib import Path

    text = _messages_to_text(messages)
    checkpoint = llm_call([
        {"role": "system", "content": (
            "You are creating a handoff document for the next agent session. "
            "The next session starts with a COMPLETELY EMPTY context window — "
            "it has zero memory of anything that happened here.\n\n"
            "Structure the handoff as:\n"
            "## Completed Work\n(what was built, with file paths)\n"
            "## Current State\n(what works, what's broken right now)\n"
            "## Next Steps\n(exactly what to do next, in order)\n"
            "## Key Decisions & Rationale\n(why things were done this way)\n"
            "## Known Issues\n(bugs, incomplete features, technical debt)\n\n"
            "Be thorough and specific — file paths, function names, error messages. "
            "The next session's success depends entirely on this document."
        )},
        {"role": "user", "content": text},
    ])

    # Persist to file
    progress_path = Path(config.WORKSPACE) / config.PROGRESS_FILE
    progress_path.write_text(checkpoint, encoding="utf-8")
    log.info(f"Checkpoint written to {config.PROGRESS_FILE}")

    return checkpoint


def restore_from_checkpoint(checkpoint: str, system_prompt: str) -> list[dict]:
    """
    Build a fresh message list from a checkpoint.
    No implicit git context injection — messages is the sole context source.
    """
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": (
            "You are resuming an in-progress project. The previous working "
            "context was rebuilt to give you a concise slate.\n\n"
            "Here is the handoff document from the previous session:\n\n"
            + checkpoint
            + "\n\nContinue from where the previous session left off. "
            "Do NOT redo work that's already completed."
        )},
    ]


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _messages_to_text(messages: list[dict]) -> str:
    """Flatten messages into readable text for summarization."""
    parts = []
    for msg in messages:
        role = msg.get("role", "?")
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = " ".join(
                block.get("text", "") for block in content if isinstance(block, dict)
            )
        if content:
            parts.append(f"[{role}] {content[:3000]}")
        for tc in msg.get("tool_calls", []):
            fn = tc.get("function", {})
            parts.append(f"[tool_call] {fn.get('name', '?')}({fn.get('arguments', '')[:500]})")
    return "\n".join(parts)


def _sanitize_message_for_rebuild(message: dict) -> dict:
    msg = dict(message)
    content = msg.get("content")
    if msg.get("role") == "tool" and isinstance(content, str):
        msg["content"] = _strip_tool_output_detail(content)
    elif isinstance(content, str) and len(content) > 2_000:
        msg["content"] = _fold_long_text(content, 2_000, label="REBUILD_CONTEXT_MESSAGE_SUMMARY")
    return msg


def _strip_tool_output_detail(content: str) -> str:
    """Strip the detail body from a tool output message.

    For OBS-formatted messages, keeps the metadata header lines only.
    For plain-text tool outputs, replaces with a compact placeholder."""
    # OBS format: keep header up to the observation:/preview boundary
    lines = content.split("\n")
    header_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("observation:") or stripped.startswith("--- preview ---"):
            break
        header_lines.append(line)
    if len(header_lines) < len(lines):
        return (
            "\n".join(header_lines)
            + "\ndetail: older full output discarded during context rebuild."
        )
    # Plain text — drop the body entirely
    digest = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:12]
    return (
        f"[tool output discarded during context rebuild]\n"
        f"original_chars: {len(content)}\n"
        f"sha256: {digest}"
    )


def _last_turn_messages(messages: list[dict], *, max_turns: int) -> list[dict]:
    if max_turns <= 0:
        return []
    user_seen = 0
    start = 1 if messages and messages[0].get("role") == "system" else 0
    for idx in range(len(messages) - 1, start - 1, -1):
        if messages[idx].get("role") == "user":
            user_seen += 1
            if user_seen >= max_turns:
                return messages[idx:]
    return messages[start:]


def _state_value(state: dict, key: str) -> str:
    value = state.get(key)
    if value is None or value == "":
        return "none"
    if isinstance(value, list):
        return _format_list(value)
    return str(value)


def _state_list(state: dict, key: str) -> str:
    value = state.get(key)
    if value is None or value == "":
        return "none"
    if isinstance(value, list):
        return _format_list(value)
    return str(value)


def _format_list(values: list) -> str:
    items = [str(item).strip() for item in values if str(item).strip()]
    return "\n".join(f"- {item}" for item in items) if items else "none"


def _fold_long_text(text: str, limit: int, *, label: str) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    head_budget = max(200, limit // 2)
    tail_budget = max(200, limit - head_budget)
    omitted = max(0, len(text) - head_budget - tail_budget)
    return (
        f"[{label}]\n"
        f"original_chars: {len(text)}\n"
        f"omitted_chars: {omitted}\n\n"
        f"{text[:head_budget]}"
        f"\n\n...[{omitted} chars omitted]...\n\n"
        f"{text[-tail_budget:]}"
    )
