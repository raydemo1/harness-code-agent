from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness_code_agent.workspace.change_journal import WorkspaceChangeJournal
from harness_code_agent.workspace.service import WorkspaceService


class WorkspaceChangeJournalTests(unittest.TestCase):
    def test_cursor_returns_only_later_changes_and_deduplicates_paths(self):
        journal = WorkspaceChangeJournal()
        journal.record("before.py", operation="write_file")
        cursor = journal.cursor()
        journal.record("after.py", operation="write_file")
        journal.record("after.py", operation="apply_patch")

        changes = journal.changes_since(cursor)

        self.assertEqual([change.sequence for change in changes], [2, 3])
        self.assertEqual([change.operation for change in changes], ["write_file", "apply_patch"])
        self.assertEqual(journal.paths_since(cursor), (Path("after.py"),))

    def test_workspace_records_operation_and_returns_an_immutable_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = WorkspaceService(root=tmp)
            workspace.write_text("note.txt", "one")
            workspace.apply_text_patch("note.txt", search="one", replace="two")

            snapshot = workspace.changed_files
            snapshot.append(Path("fake.txt"))
            changes = workspace.change_journal.changes_since()

            self.assertEqual(workspace.changed_files, [Path("note.txt")])
            self.assertEqual([change.operation for change in changes], ["write_file", "apply_patch"])
            self.assertIsNotNone(changes[1].snapshot_path)


if __name__ == "__main__":
    unittest.main()
