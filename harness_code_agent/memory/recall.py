from __future__ import annotations

from dataclasses import dataclass

from ..runtime.tool_search import SearchDocument, build_bm25_retriever, search_bm25
from .query import MemoryQueryComposer
from .store import MemoryRecord, MemoryStore, _env_float, _env_int


DEFAULT_TOP_K = 6
DEFAULT_MIN_SCORE = 0.3


@dataclass(frozen=True)
class MemoryHit:
    record: MemoryRecord
    score: float


class MemoryRecall:
    def __init__(self, store: MemoryStore):
        self.store = store
        self.composer = MemoryQueryComposer()
        self._cached_mtime: float | None = None
        self._cached_documents: list[SearchDocument] = []
        self._cached_records: list[MemoryRecord] = []
        self._cached_retriever = None

    def search(
        self,
        user_turn: str,
        *,
        mentions: list[str] | None = None,
        top_k: int | None = None,
        min_score: float | None = None,
    ) -> list[MemoryHit]:
        if not self.store.exists():
            return []
        query = self.composer.compose(
            user_turn,
            mentions=mentions or [],
        )
        if not query.should_recall:
            return []
        self._maybe_rebuild_cache()
        if not self._cached_documents:
            return []
        limit = top_k if top_k is not None else _env_int("HARNESS_MEMORY_TOP_K", DEFAULT_TOP_K)
        threshold = (
            min_score
            if min_score is not None
            else _env_float("HARNESS_MEMORY_MIN_SCORE", DEFAULT_MIN_SCORE)
        )
        raw_hits = search_bm25(
            self._cached_documents,
            query.text,
            limit=limit,
            retriever=self._cached_retriever,
            allow_index_build=False,
        )
        if not raw_hits or raw_hits[0].score < threshold:
            return []

        record_map = {record.id: record for record in self._cached_records}
        hits: list[MemoryHit] = []
        for hit in raw_hits:
            if hit.score <= 0 or hit.score < threshold:
                continue
            record = record_map.get(hit.key)
            if record is None or record.status != "active":
                continue
            hits.append(MemoryHit(record=record, score=hit.score))
        return hits[:limit]

    def format_block(self, hits: list[MemoryHit]) -> str:
        if not hits:
            return ""
        lines = [
            "Relevant long-term memory:",
            "Use these notes before searching; if they fully answer the user turn, answer from them and inspect files only to fill gaps.",
        ]
        lines.extend(self.format_hit_lines(hits))
        return "\n".join(lines)

    def format_hit_lines(self, hits: list[MemoryHit]) -> list[str]:
        lines: list[str] = []
        for hit in hits:
            record = hit.record
            tags = ", ".join(record.tags[:6]) or "-"
            paths = ", ".join(record.source_paths[:4]) or "-"
            lines.append(
                f"- [{record.id}] {record.title} ({record.file}#{record.anchor}, score={hit.score:.2f})"
            )
            lines.append(f"  Summary: {record.summary}")
            lines.append(f"  Tags: {tags}")
            lines.append(f"  Source paths: {paths}")
        return lines

    def _maybe_rebuild_cache(self) -> None:
        records_path = self.store.root / "records.jsonl"
        if not records_path.exists():
            self._cached_mtime = None
            self._cached_documents = []
            self._cached_records = []
            self._cached_retriever = None
            return
        mtime = records_path.stat().st_mtime
        if self._cached_mtime == mtime:
            return

        records = _active_unique_records(self.store.read_records())
        documents = [
            SearchDocument(
                key=record.id,
                text=_record_text(record),
                metadata={"record_id": record.id},
            )
            for record in records
        ]
        self._cached_mtime = mtime
        self._cached_documents = documents
        self._cached_records = records
        self._cached_retriever = build_bm25_retriever(documents)


def _active_unique_records(records: list[MemoryRecord]) -> list[MemoryRecord]:
    seen: set[str] = set()
    active: list[MemoryRecord] = []
    superseded = {record_id for record in records for record_id in record.supersedes}
    for record in records:
        if record.id in seen:
            continue
        seen.add(record.id)
        if record.status != "active" or record.superseded_by or record.id in superseded:
            continue
        active.append(record)
    return active


def _record_text(record: MemoryRecord) -> str:
    return " ".join(
        [
            record.title,
            record.summary,
            record.file,
            record.anchor,
            " ".join(record.tags),
            " ".join(record.source_paths),
        ]
    )
