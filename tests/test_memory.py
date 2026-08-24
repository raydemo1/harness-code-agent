import builtins
import inspect
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.memory_root = self.temp_dir / "memory"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_ensure_initialized_creates_flat_files_and_manifest(self):
        from harness_code_agent.memory.store import MEMORY_FILES, MemoryStore

        store = MemoryStore(self.memory_root, workspace=self.temp_dir)
        store.ensure_initialized()

        for filename in MEMORY_FILES:
            self.assertTrue((self.memory_root / filename).exists(), filename)
        self.assertTrue((self.memory_root / "preferences.md").exists())
        self.assertFalse((self.memory_root / "preference.md").exists())
        manifest = json.loads((self.memory_root / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["repo_path"], str(self.temp_dir.resolve()))

    def test_repo_identity_probe_times_out_and_falls_back_to_workspace(self):
        from harness_code_agent.memory.store import resolve_repo_key

        with patch(
            "harness_code_agent.memory.store.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["git", "rev-parse"], 0.5),
        ) as run:
            key = resolve_repo_key(self.temp_dir)

        self.assertTrue(key.startswith(f"{self.temp_dir.name}-"))
        self.assertLessEqual(run.call_args.kwargs["timeout"], 0.5)
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")

    def test_read_memory_file_rejects_path_traversal(self):
        from harness_code_agent.memory.store import MemoryStore

        store = MemoryStore(self.memory_root, workspace=self.temp_dir)
        store.ensure_initialized()

        with self.assertRaises(ValueError):
            store.read_memory_file("../records.jsonl")

    def test_append_candidate_writes_inbox_not_records(self):
        from harness_code_agent.memory.store import MemoryStore

        store = MemoryStore(self.memory_root, workspace=self.temp_dir)
        store.append_candidate({"summary": "Use PowerShell commands on Windows.", "file": "preferences.md"})

        self.assertEqual(len(store.read_inbox()), 1)
        self.assertEqual(store.read_records(), [])


class MemoryDreamTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.store = self._store()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _store(self):
        from harness_code_agent.memory.store import MemoryStore

        return MemoryStore(self.temp_dir / "memory", workspace=self.temp_dir)

    def test_dream_merges_candidates_writes_markdown_and_clears_inbox(self):
        from harness_code_agent.memory.dream import run_dream

        self.store.append_candidate(
            {
                "title": "Windows command preference",
                "summary": "Prefer PowerShell syntax and explicit UTF-8 encoding for text files.",
                "file": "preferences.md",
                "tags": ["preference", "command"],
            }
        )

        summary = run_dream(self.store)

        self.assertIn("merged 1 candidates", summary)
        self.assertEqual(self.store.read_inbox(), [])
        records = self.store.read_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].status, "active")
        self.assertIn("Windows command preference", self.store.read_memory_file("preferences.md"))
        self.assertIn("preferences.md", self.store.read_memory_file("MEMORY.md"))
        self.assertIn(records[0].id, self.store.read_memory_file("dream-log.md"))

    def test_dream_marks_conflicting_batch_record_superseded(self):
        from harness_code_agent.memory.dream import run_dream

        self.store.append_candidate(
            {
                "title": "Test command",
                "summary": "Run python -m unittest for the project.",
                "file": "commands.md",
                "anchor": "test-command",
            }
        )
        self.store.append_candidate(
            {
                "title": "Test command",
                "summary": "Run python -m unittest discover -s tests -p test_*.py.",
                "file": "commands.md",
                "anchor": "test-command",
            }
        )

        run_dream(self.store)

        records = self.store.read_records()
        self.assertEqual([record.status for record in records].count("active"), 1)
        self.assertEqual([record.status for record in records].count("superseded"), 1)
        active = next(record for record in records if record.status == "active")
        old = next(record for record in records if record.status == "superseded")
        self.assertEqual(active.supersedes, [old.id])
        self.assertEqual(old.superseded_by, active.id)


class MemoryRecallTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        from harness_code_agent.memory.store import MemoryStore

        self.store = MemoryStore(self.temp_dir / "memory", workspace=self.temp_dir)
        self.store.ensure_initialized()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_recall_uses_bm25_top6_and_filters_low_scores(self):
        from harness_code_agent.memory.recall import MemoryRecall
        from harness_code_agent.memory.store import MemoryRecord

        records = []
        for idx in range(8):
            records.append(
                MemoryRecord(
                    id=f"mem_{idx}",
                    file="debugging.md",
                    anchor=f"anchor-{idx}",
                    title=f"Parser debug note {idx}",
                    summary=f"Parser failure around token stream handling case {idx}.",
                    tags=["debug"],
                    created_at="2026-01-01T00:00:00Z",
                    updated_at=f"2026-01-01T00:00:0{idx}Z",
                )
            )
        records.append(
            MemoryRecord(
                id="mem_unrelated",
                file="preferences.md",
                anchor="style",
                title="Output style",
                summary="Keep final answers concise.",
                status="active",
            )
        )
        records.append(
            MemoryRecord(
                id="mem_old",
                file="debugging.md",
                anchor="old",
                title="Old parser note",
                summary="Parser failure note replaced by newer record.",
                status="superseded",
            )
        )
        self.store.atomic_write_records(records)

        hits = MemoryRecall(self.store).search("debug parser token failure", min_score=0.1)

        self.assertEqual(len(hits), 6)
        self.assertTrue(all(hit.record.status == "active" for hit in hits))
        self.assertNotIn("mem_unrelated", {hit.record.id for hit in hits})
        self.assertNotIn("mem_old", {hit.record.id for hit in hits})

        no_hits = MemoryRecall(self.store).search("billing invoice export", min_score=1.0)
        self.assertEqual(no_hits, [])

    def test_memory_block_tells_agent_to_use_memory_before_searching(self):
        from harness_code_agent.memory.recall import MemoryHit, MemoryRecall
        from harness_code_agent.memory.store import MemoryRecord

        recall = MemoryRecall(self.store)
        block = recall.format_block(
            [
                MemoryHit(
                    record=MemoryRecord(
                        id="mem_1",
                        file="project.md",
                        anchor="cache",
                        title="Cache note",
                        summary="Cache diagnostics live in harness_code_agent/agent/utils.py.",
                        status="active",
                    ),
                    score=1.0,
                )
            ]
        )

        self.assertIn("Use these notes before searching", block)
        self.assertIn("inspect files only to fill gaps", block)
        self.assertIn("harness_code_agent/agent/utils.py", block)

    def test_small_memory_corpus_does_not_import_bm25(self):
        from harness_code_agent.runtime.tool_search import SearchDocument, search_bm25

        documents = [
            SearchDocument(
                key="mem_1",
                text="Session observability aggregates cached_tokens and cache_miss_tokens.",
            )
        ]
        real_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name == "BM25":
                raise AssertionError("small corpus search should not import BM25")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=guarded_import):
            hits = search_bm25(documents, "observability cache tokens", limit=3)

        self.assertEqual([hit.key for hit in hits], ["mem_1"])

    def test_memory_recall_reuses_bm25_index_until_records_change(self):
        from harness_code_agent.memory.recall import MemoryRecall
        from harness_code_agent.memory.store import MemoryRecord
        from harness_code_agent.runtime.tool_search import SearchHit

        records = [
            MemoryRecord(
                id=f"mem_{idx}",
                file="project.md",
                anchor=f"cache-{idx}",
                title=f"Cache note {idx}",
                summary=f"Prompt cache diagnostic note {idx}.",
                tags=["cache"],
                status="active",
            )
            for idx in range(2)
        ]
        self.store.atomic_write_records(records)
        fake_retriever = object()

        def fake_search(documents, query, *, limit=8, retriever=None, **kwargs):
            self.assertIs(retriever, fake_retriever)
            self.assertFalse(kwargs.get("allow_index_build", True))
            return [
                SearchHit(
                    key="mem_0",
                    score=2.0,
                    document=documents[0].text,
                    metadata=dict(documents[0].metadata),
                )
            ]

        with (
            patch.dict("os.environ", {"HARNESS_BM25_MIN_DOCS": "1"}),
            patch("harness_code_agent.memory.recall.build_bm25_retriever", return_value=fake_retriever) as build,
            patch("harness_code_agent.memory.recall.search_bm25", side_effect=fake_search) as search,
        ):
            recall = MemoryRecall(self.store)
            first = recall.search("cache diagnostic", min_score=0.1)
            second = recall.search("cache diagnostic", min_score=0.1)

        self.assertEqual([hit.record.id for hit in first], ["mem_0"])
        self.assertEqual([hit.record.id for hit in second], ["mem_0"])
        self.assertEqual(build.call_count, 1)
        self.assertEqual(search.call_count, 2)


class MemoryQueryTests(unittest.TestCase):
    def test_query_composer_does_not_accept_profile_or_repo_name(self):
        from harness_code_agent.memory.query import MemoryQueryComposer

        signature = inspect.signature(MemoryQueryComposer().compose)

        self.assertNotIn("profile", signature.parameters)
        self.assertNotIn("repo_name", signature.parameters)


class MemoryMiddlewareTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_memory_index_injection_is_marked_and_truncated(self):
        from harness_code_agent.memory.store import MemoryRecord, MemoryStore
        from harness_code_agent.runtime.middleware.memory import (
            MEMORY_INDEX_MARKER,
            MemoryMiddleware,
        )

        store = MemoryStore(self.temp_dir / "memory", workspace=self.temp_dir)
        store.ensure_initialized()
        store.atomic_write_records([
            MemoryRecord(
                id="mem_1",
                file="commands.md",
                anchor="test",
                title="Test command",
                summary="Run targeted unittest commands.",
                status="active",
            )
        ])
        store.atomic_write("MEMORY.md", "\n".join(f"line {idx}" for idx in range(10)))
        middleware = MemoryMiddleware(workspace=self.temp_dir)

        with (
            patch.object(middleware, "_store", return_value=store),
            patch("harness_code_agent.memory.dream.should_dream", return_value=False),
            patch.dict(
                "os.environ",
                {
                    "HARNESS_MEMORY_DISABLED": "",
                    "HARNESS_MEMORY_INDEX_MAX_LINES": "3",
                    "HARNESS_MEMORY_INDEX_MAX_CHARS": "1000",
                },
            ),
        ):
            messages = middleware.on_conversation_start([])

        self.assertEqual(len(messages), 1)
        content = messages[0]["content"]
        self.assertEqual(messages[0]["role"], "system")
        self.assertTrue(content.startswith(MEMORY_INDEX_MARKER))
        self.assertIn("exact tools memory_search and read_memory_file", content)
        self.assertNotIn("memory_read", content)
        self.assertIn("line 0", content)
        self.assertIn("line 2", content)
        self.assertNotIn("line 3", content)
        self.assertIn("truncated", content.lower())

    def test_memory_middleware_reuses_recall_instance_for_same_store(self):
        from harness_code_agent.memory.store import MemoryRecord, MemoryStore
        from harness_code_agent.runtime.middleware.memory import MemoryMiddleware

        store = MemoryStore(self.temp_dir / "memory", workspace=self.temp_dir)
        store.ensure_initialized()
        store.atomic_write_records([
            MemoryRecord(
                id="mem_1",
                file="project.md",
                anchor="cache",
                title="Cache note",
                summary="Prompt cache diagnostic note.",
                status="active",
            )
        ])
        middleware = MemoryMiddleware(workspace=self.temp_dir)
        recall = SimpleNamespace(
            search=Mock(return_value=[]),
            format_block=Mock(return_value=""),
        )

        with (
            patch.object(middleware, "_store", return_value=store),
            patch("harness_code_agent.memory.dream.should_dream", return_value=False),
            patch("harness_code_agent.memory.recall.MemoryRecall", return_value=recall) as recall_cls,
            patch.dict("os.environ", {"HARNESS_MEMORY_DISABLED": ""}),
        ):
            middleware.augment_user_prompt("cache", [], mention_paths=[])
            middleware.augment_user_prompt("cache", [], mention_paths=[])

        self.assertEqual(recall_cls.call_count, 1)
        self.assertEqual(recall.search.call_count, 2)


class MemoryToolTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_remember_memory_rejects_protected_file(self):
        from harness_code_agent.runtime.builtins.memory_tools import remember_memory

        context = SimpleNamespace(workspace=SimpleNamespace(root=self.temp_dir), session_id="s1")

        result = remember_memory(
            summary="Do not write protected files directly.",
            file="MEMORY.md",
            tool_context=context,
        )

        self.assertEqual(result.status, "failed")
        self.assertIn("protected", result.error)

    def test_memory_search_returns_summaries(self):
        from harness_code_agent.memory.store import MemoryRecord, MemoryStore
        from harness_code_agent.runtime.builtins.memory_tools import memory_search

        memory_root = self.temp_dir / "memory"
        store = MemoryStore(memory_root, workspace=self.temp_dir)
        store.ensure_initialized()
        store.atomic_write_records(
            [
                MemoryRecord(
                    id="mem_search_1",
                    file="debugging.md",
                    anchor="parser-token-failure",
                    title="Parser token failure",
                    summary="Parser token stream failures should be debugged with fixture replay.",
                    tags=["debug"],
                    status="active",
                )
            ]
        )
        context = SimpleNamespace(workspace=SimpleNamespace(root=self.temp_dir), session_id="s1")

        with patch.dict("os.environ", {"HARNESS_MEMORY_ROOT": str(memory_root)}):
            result = memory_search("debug parser token failure", tool_context=context)

        self.assertEqual(result.status, "success")
        self.assertIn("mem_search_1", result.output)
        self.assertIn("read_memory_file", result.output)


class MemoryPromptTests(unittest.TestCase):
    def test_turn_format_order_is_mentions_memory_user_turn(self):
        from harness_code_agent.core.interactive import (
            _format_turn_with_mentions_and_memory,
        )
        from harness_code_agent.core.mentions import ResolvedMention

        resolved = [
            ResolvedMention(
                raw="@README.md",
                kind="file",
                target="README.md",
                resolved="C:/repo/README.md",
                content="kind: file",
            )
        ]

        text = _format_turn_with_mentions_and_memory(
            "fix this",
            resolved,
            "Relevant long-term memory:\n- [mem_1] note",
        )

        self.assertLess(text.index("Mention context:"), text.index("Relevant long-term memory:"))
        self.assertLess(text.index("Relevant long-term memory:"), text.index("User turn:"))
