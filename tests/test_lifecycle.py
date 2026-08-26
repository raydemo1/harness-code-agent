from __future__ import annotations

import unittest

from harness_code_agent.runtime.lifecycle import LifecycleScope


class LifecycleScopeTests(unittest.TestCase):
    def test_closes_higher_order_and_newer_resources_first(self):
        closed: list[str] = []
        lifecycle = LifecycleScope()
        lifecycle.register("mcp-old", lambda: closed.append("mcp-old"), order=10)
        lifecycle.register("mcp-new", lambda: closed.append("mcp-new"), order=10)
        lifecycle.register("conversation", lambda: closed.append("conversation"), order=40)

        self.assertEqual(lifecycle.close(), [])
        self.assertEqual(closed, ["conversation", "mcp-new", "mcp-old"])

    def test_cleanup_continues_after_one_resource_fails(self):
        closed: list[str] = []
        lifecycle = LifecycleScope()
        lifecycle.register("last", lambda: closed.append("last"), order=10)

        def fail() -> None:
            raise RuntimeError("broken close")

        lifecycle.register("broken", fail, order=20)
        lifecycle.register("first", lambda: closed.append("first"), order=30)

        errors = lifecycle.close()

        self.assertEqual(closed, ["first", "last"])
        self.assertEqual([(error.name, error.error) for error in errors], [
            ("broken", "RuntimeError: broken close"),
        ])

    def test_close_is_idempotent_and_rejects_late_registration(self):
        closed: list[str] = []
        lifecycle = LifecycleScope()
        lifecycle.register("resource", lambda: closed.append("resource"), order=10)

        lifecycle.close()
        self.assertEqual(lifecycle.close(), [])
        self.assertEqual(closed, ["resource"])
        with self.assertRaisesRegex(RuntimeError, "closed"):
            lifecycle.register("late", lambda: None, order=10)


if __name__ == "__main__":
    unittest.main()
