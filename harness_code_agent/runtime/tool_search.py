from __future__ import annotations

import contextlib
import io
import os
import re
from dataclasses import dataclass, field
from typing import Any

DEFAULT_BM25_MIN_DOCUMENTS = 33


@dataclass(frozen=True)
class SearchDocument:
    key: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SearchHit:
    key: str
    score: float
    document: str
    metadata: dict[str, Any] = field(default_factory=dict)


def search_bm25(
    documents: list[SearchDocument],
    query: str,
    *,
    limit: int = 8,
    retriever: Any | None = None,
    allow_index_build: bool = True,
) -> list[SearchHit]:
    """Search documents with the BM25 package behind a tiny stable boundary."""
    query = str(query or "").strip()
    if not query or not documents or limit <= 0:
        return []

    if retriever is None and allow_index_build:
        retriever = build_bm25_retriever(documents)
    if retriever is None:
        return _fallback_search(documents, query, limit=limit)

    # BM25 delegates to bm25s, which emits progress bars by default. Keep that
    # noise out of tool output and tests.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        raw_results = retriever.search([query], k=min(limit, len(documents)))

    return _hits_from_bm25_results(documents, raw_results)


def build_bm25_retriever(documents: list[SearchDocument]) -> Any | None:
    """Build a reusable BM25 index when the corpus is large enough to justify it."""
    if not documents or len(documents) < _bm25_min_documents():
        return None
    try:
        import BM25
    except ImportError:
        return None

    corpus = [document.text for document in documents]
    # BM25 delegates to bm25s, which emits progress bars by default. Keep that
    # noise out of tool output and tests.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return BM25.index(corpus)


def _hits_from_bm25_results(documents: list[SearchDocument], raw_results: Any) -> list[SearchHit]:
    hits: list[SearchHit] = []
    for item in (raw_results[0] if raw_results else []):
        try:
            doc_id = int(item.get("id"))
            score = float(item.get("score") or 0.0)
        except (TypeError, ValueError):
            continue
        if score <= 0 or doc_id < 0 or doc_id >= len(documents):
            continue
        document = documents[doc_id]
        hits.append(
            SearchHit(
                key=document.key,
                score=score,
                document=document.text,
                metadata=dict(document.metadata),
            )
        )
    return hits


def _bm25_min_documents() -> int:
    value = os.environ.get("HARNESS_BM25_MIN_DOCS", "").strip()
    if not value:
        return DEFAULT_BM25_MIN_DOCUMENTS
    try:
        return max(1, int(value))
    except ValueError:
        return DEFAULT_BM25_MIN_DOCUMENTS


def expand_search_text(value: str) -> str:
    """Make schema-ish names searchable without binding to specific tools."""
    value = str(value or "")
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    tokens = [token for token in re.split(r"[^A-Za-z0-9]+", spaced) if token]
    return " ".join([value, *tokens])


def _fallback_search(documents: list[SearchDocument], query: str, *, limit: int) -> list[SearchHit]:
    query_terms = {term.lower() for term in expand_search_text(query).split() if term}
    if not query_terms:
        return []
    hits: list[SearchHit] = []
    for document in documents:
        text = expand_search_text(document.text).lower()
        score = sum(1.0 for term in query_terms if term in text)
        if score <= 0:
            continue
        hits.append(SearchHit(document.key, score, document.text, dict(document.metadata)))
    hits.sort(key=lambda item: (-item.score, item.key))
    return hits[:limit]
