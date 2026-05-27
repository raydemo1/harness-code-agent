import unittest

from harness_code_agent.runtime.tools import _smart_truncate_output


class SmartTruncateOutputTests(unittest.TestCase):
    """Tests for _smart_truncate_output — run_bash output truncation."""

    def test_short_output_not_truncated(self):
        stdout = "hello world"
        stderr = ""
        result = _smart_truncate_output(stdout, stderr)
        self.assertEqual(result, "hello world")

    def test_short_output_with_stderr_not_truncated(self):
        stdout = "line1\nline2"
        stderr = "warn: something"
        result = _smart_truncate_output(stdout, stderr)
        self.assertIn("line1\nline2", result)
        self.assertIn("warn: something", result)
        # Short output returns combined without separator
        self.assertNotIn("--- STDERR ---", result)

    def test_default_limit_is_12000(self):
        stdout = "x" * 15_000
        stderr = ""
        result = _smart_truncate_output(stdout, stderr)
        self.assertLessEqual(len(result), 12_000)

    def test_truncation_preserves_head_and_tail(self):
        stdout = "HEAD_START\n" + "x" * 10_000 + "\nTAIL_END"
        stderr = ""
        result = _smart_truncate_output(stdout, stderr)
        self.assertIn("HEAD_START", result)
        self.assertIn("TAIL_END", result)
        self.assertLessEqual(len(result), 12_000)

    def test_stderr_preserves_tail(self):
        stdout = "out"
        stderr = "begin\n" + "e" * 15_000 + "\nTAIL_ERROR"
        result = _smart_truncate_output(stdout, stderr)
        self.assertIn("--- STDERR ---", result)
        self.assertIn("TAIL_ERROR", result)
        self.assertLessEqual(len(result), 12_000)

    def test_important_lines_extracted_from_middle(self):
        middle_lines = "\n".join(f"line {i}" for i in range(500))
        middle_lines += "\nFATAL ERROR in module X\n"
        middle_lines += "\n".join(f"line {500 + i}" for i in range(500))
        stdout = "HEAD\n" + middle_lines + "\nTAIL"
        stderr = ""
        result = _smart_truncate_output(stdout, stderr)
        self.assertIn("FATAL ERROR in module X", result)

    def test_custom_limit_respected(self):
        stdout = "x" * 5_000
        stderr = ""
        result = _smart_truncate_output(stdout, stderr, limit=1_000)
        self.assertLessEqual(len(result), 1_000)

    def test_empty_input(self):
        self.assertEqual(_smart_truncate_output("", ""), "")

    def test_only_stderr_short(self):
        stderr = "error output"
        result = _smart_truncate_output("", stderr)
        self.assertIn("error output", result)

    def test_only_stderr_long_truncated(self):
        stderr = "e" * 15_000
        result = _smart_truncate_output("", stderr)
        self.assertIn("--- STDERR ---", result)
        self.assertLessEqual(len(result), 12_000)


if __name__ == "__main__":
    unittest.main()
