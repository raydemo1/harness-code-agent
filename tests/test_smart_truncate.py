import unittest

from harness_code_agent.runtime.tools import _build_shell_output


class BuildShellOutputTests(unittest.TestCase):
    """Tests for _build_shell_output — run_bash output construction."""

    def test_short_output_not_truncated(self):
        stdout = "hello world"
        stderr = ""
        result = _build_shell_output(stdout, stderr)
        self.assertEqual(result, "hello world")

    def test_short_output_with_stderr_not_truncated(self):
        stdout = "line1\nline2"
        stderr = "warn: something"
        result = _build_shell_output(stdout, stderr)
        self.assertIn("line1\nline2", result)
        self.assertIn("warn: something", result)
        self.assertIn("--- STDERR ---", result)
        self.assertEqual(result, "line1\nline2\n\n--- STDERR ---\nwarn: something")

    def test_large_output_passes_through_unchanged(self):
        stdout = "x" * 150_000
        stderr = ""
        result = _build_shell_output(stdout, stderr)
        self.assertEqual(len(result), 150_000)

    def test_output_over_200k_is_not_truncated(self):
        stdout = "HEAD_START\n" + "x" * 125_000 + "MIDDLE_SENTINEL" + "y" * 125_000 + "\nTAIL_END"
        stderr = ""
        result = _build_shell_output(stdout, stderr)
        self.assertIn("HEAD_START", result)
        self.assertIn("MIDDLE_SENTINEL", result)
        self.assertIn("TAIL_END", result)
        self.assertEqual(result, stdout)

    def test_stderr_passes_through_unchanged(self):
        stdout = "out"
        stderr = "begin\n" + "e" * 125_000 + "STDERR_MIDDLE" + "f" * 125_000 + "\nTAIL_ERROR"
        result = _build_shell_output(stdout, stderr)
        self.assertIn("out", result)
        self.assertIn("--- STDERR ---", result)
        self.assertIn("STDERR_MIDDLE", result)
        self.assertIn("TAIL_ERROR", result)

    def test_important_lines_remain_in_middle(self):
        middle_lines = "\n".join(f"line {i}" for i in range(8000))
        middle_lines += "\nFATAL ERROR in module X\n"
        middle_lines += "\n".join(f"line {8000 + i}" for i in range(8000))
        stdout = "HEAD\n" + middle_lines + "\nTAIL"
        stderr = ""
        result = _build_shell_output(stdout, stderr)
        self.assertIn("FATAL ERROR in module X", result)

    def test_empty_input(self):
        self.assertEqual(_build_shell_output("", ""), "")

    def test_only_stderr_short(self):
        stderr = "error output"
        result = _build_shell_output("", stderr)
        self.assertEqual(result, "--- STDERR ---\nerror output")

    def test_only_stderr_long_not_truncated(self):
        stderr = "e" * 250_000
        result = _build_shell_output("", stderr)
        self.assertEqual(result, "--- STDERR ---\n" + stderr)


if __name__ == "__main__":
    unittest.main()
