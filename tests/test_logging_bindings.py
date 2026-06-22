import ast
import unittest
from pathlib import Path


class LoggingBindingTests(unittest.TestCase):
    def test_modules_using_log_define_it_at_module_scope(self):
        missing = []
        for path in Path("harness_code_agent").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            uses_log = any(
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "log"
                for node in ast.walk(tree)
            )
            if not uses_log:
                continue

            module_names = set()
            for node in tree.body:
                if isinstance(node, ast.Assign):
                    targets = node.targets
                elif isinstance(node, ast.AnnAssign):
                    targets = [node.target]
                else:
                    continue
                module_names.update(
                    target.id for target in targets if isinstance(target, ast.Name)
                )
            if "log" not in module_names:
                missing.append(str(path))

        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
