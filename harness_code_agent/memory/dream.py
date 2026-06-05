from __future__ import annotations

import json
import os
import re
import time

from .navigator import rebuild_navigation_from_records
from .store import MEMORY_CONTENT_FILES, MemoryRecord, MemoryStore


DEFAULT_INBOX_THRESHOLD = 12


def should_dream(store: MemoryStore) -> bool:
    if not store.exists():
        return False
    inbox = store.read_inbox()
    threshold = _env_int("HARNESS_MEMORY_INBOX_THRESHOLD", DEFAULT_INBOX_THRESHOLD)
    if len(inbox) >= threshold:
        return True
    manifest = store.read_manifest()
    last = str(manifest.get("last_dream_at") or "")
    interval_seconds = _env_int("HARNESS_MEMORY_DREAM_INTERVAL_HOURS", 24) * 60 * 60
    if inbox and _seconds_since(last) >= interval_seconds:
        return True
    return False


def run_dream(store: MemoryStore) -> str:
    store.ensure_initialized()
    with store.lock():
        inbox = store.read_inbox()
        existing_records = store.read_records()
        if not inbox:
            return "Dream skipped: inbox is empty."

        now = _utc_now()
        current_active_records = [record for record in existing_records if record.status == "active"]
        new_records: list[MemoryRecord] = []
        superseded_ids: set[str] = set()
        skipped_count = 0

        for candidate in inbox:
            title = _clean_text(candidate.get("title") or candidate.get("summary") or "Memory", max_len=80)
            summary = _clean_text(candidate.get("summary") or "", max_len=700)
            if not summary:
                skipped_count += 1
                continue
            target_file = str(candidate.get("file") or _route_file(candidate)).strip()
            if target_file not in MEMORY_CONTENT_FILES:
                target_file = _route_file(candidate)
            anchor = str(candidate.get("anchor") or _anchor(title))
            conflicting = _find_conflict(candidate, target_file, anchor, current_active_records)
            if conflicting:
                superseded_ids.add(conflicting.id)
                current_active_records = [
                    record for record in current_active_records if record.id != conflicting.id
                ]

            record = MemoryRecord(
                id=_new_record_id(existing_records, new_records),
                file=target_file,
                anchor=anchor,
                title=title,
                summary=summary,
                tags=_string_list(candidate.get("tags")),
                source_paths=_string_list(candidate.get("source_paths")),
                source_sessions=_string_list(candidate.get("source_sessions")),
                confidence=float(candidate.get("confidence") or 0.5),
                status="active",
                supersedes=[conflicting.id] if conflicting else [],
                created_at=now,
                updated_at=now,
            )
            new_records.append(record)
            current_active_records.append(record)

        updated: list[MemoryRecord] = []
        for record in [*existing_records, *new_records]:
            if record.id in superseded_ids:
                replacement = next((new for new in new_records if record.id in new.supersedes), None)
                record.status = "superseded"
                record.superseded_by = replacement.id if replacement else ""
                record.updated_at = now
            updated.append(record)

        active = [record for record in updated if record.status == "active" and not record.superseded_by]
        summary = (
            f"Dream run at {now}: merged {len(new_records)} candidates, "
            f"superseded {len(superseded_ids)} records, skipped {skipped_count}."
        )
        markdown_updates = _build_markdown_updates(active)
        nav = rebuild_navigation_from_records(active, dream_summary=summary)
        manifest = store.read_manifest()
        manifest.update(
            {
                "updated_at": now,
                "last_dream_at": now,
                "active_record_count": len(active),
                "inbox_count": 0,
            }
        )
        dream_log = _append_dream_log(store.read_memory_file("dream-log.md"), summary, new_records, superseded_ids)

        for filename, content in markdown_updates.items():
            store.atomic_write(filename, content)
        store.atomic_write_records(updated)
        store.atomic_write("MEMORY.md", nav)
        store.atomic_write("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
        store.atomic_write("dream-log.md", dream_log)
        store.clear_inbox()
        return summary


def _find_conflict(
    candidate: dict,
    target_file: str,
    anchor: str,
    active_records: list[MemoryRecord],
) -> MemoryRecord | None:
    candidate_paths = set(_string_list(candidate.get("source_paths")))
    for record in active_records:
        if record.status != "active":
            continue
        if record.file == target_file and record.anchor == anchor:
            return record
        if candidate_paths and record.file == target_file and candidate_paths & set(record.source_paths):
            return record
    return None


def _route_file(candidate: dict) -> str:
    text = " ".join(
        [
            str(candidate.get("file") or ""),
            str(candidate.get("title") or ""),
            str(candidate.get("summary") or ""),
            " ".join(_string_list(candidate.get("tags"))),
        ]
    ).lower()
    if any(word in text for word in ("preference", "prefer", "偏好", "要求", "习惯")):
        return "preferences.md"
    if any(word in text for word in ("command", "shell", "powershell", "命令")):
        return "commands.md"
    if any(word in text for word in ("debug", "bug", "error", "失败", "报错")):
        return "debugging.md"
    if any(word in text for word in ("decision", "tradeoff", "rationale", "决策", "取舍")):
        return "decisions.md"
    if any(word in text for word in ("architecture", "module", "project", "架构", "模块")):
        return "project.md"
    return "learnings.md"


def _build_markdown_updates(active_records: list[MemoryRecord]) -> dict[str, str]:
    grouped = {filename: [] for filename in MEMORY_CONTENT_FILES}
    for record in active_records:
        grouped.setdefault(record.file, []).append(record)
    updates: dict[str, str] = {}
    for filename in MEMORY_CONTENT_FILES:
        records = sorted(grouped.get(filename, []), key=lambda record: (record.anchor, record.id))
        lines = [f"# {filename}", ""]
        if not records:
            lines.append("_No active records._")
        for record in records:
            lines.extend(
                [
                    f"## {record.title}",
                    "",
                    f"- id: {record.id}",
                    f"- anchor: {record.anchor}",
                    f"- updated_at: {record.updated_at}",
                    f"- tags: {', '.join(record.tags) or '-'}",
                    f"- source_paths: {', '.join(record.source_paths) or '-'}",
                    "",
                    record.summary,
                    "",
                ]
            )
        updates[filename] = "\n".join(lines).rstrip() + "\n"
    return updates


def _append_dream_log(previous: str, summary: str, records: list[MemoryRecord], superseded_ids: set[str]) -> str:
    lines = [previous.rstrip(), "", f"## {summary}", ""]
    if records:
        lines.append("Merged:")
        for record in records:
            lines.append(f"- {record.id}: {record.title}")
    if superseded_ids:
        lines.append("")
        lines.append("Superseded:")
        for record_id in sorted(superseded_ids):
            lines.append(f"- {record_id}")
    return "\n".join(line for line in lines if line is not None).lstrip() + "\n"


def _new_record_id(existing: list[MemoryRecord], new_records: list[MemoryRecord]) -> str:
    today = time.strftime("%Y%m%d", time.gmtime())
    used = {record.id for record in existing + new_records}
    idx = len(used) + 1
    while True:
        candidate = f"mem_{today}_{idx:04d}"
        if candidate not in used:
            return candidate
        idx += 1


def _anchor(title: str) -> str:
    value = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", title.lower()).strip("-")
    return value[:80] or "memory"


def _clean_text(value: object, *, max_len: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:max_len]


def _string_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _seconds_since(timestamp: str) -> float:
    if not timestamp:
        return float("inf")
    try:
        parsed = time.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return float("inf")
    return time.time() - time.mktime(parsed)


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
