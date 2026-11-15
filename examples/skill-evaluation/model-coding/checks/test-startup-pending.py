# SPDX-License-Identifier: AGPL-3.0-only
import importlib.util
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("solution", "/workspace/benchmark.py")
solution = importlib.util.module_from_spec(spec)
spec.loader.exec_module(solution)


class StartupChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "USER.md").write_text("- Authoritative preference \u03bb\n")
        (self.root / "projects/demo").mkdir(parents=True)
        (self.root / "projects/demo/MEMORY.md").write_text("- Keep project facts\n\n")

    def test_pending_failures_keep_exact_memory_and_unknown_count(self):
        for code in ("too_many_items", "unsafe_path", "changed"):
            calls = []

            def unavailable(code=code, calls=calls):
                calls.append(1)
                raise solution.PendingUnavailable(code)

            before = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
            result = solution.snapshot(self.root, "demo", unavailable)
            self.assertEqual(result, {"user": "- Authoritative preference \u03bb\n",
                                      "project": "- Keep project facts\n\n",
                                      "pending_count": None, "pending_error": code})
            self.assertEqual(calls, [1])
            self.assertEqual({str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}, before)

    def test_empty_and_nonempty_complete_listing(self):
        for ids in ([], ["a"], ["a", "b", "c"]):
            result = solution.snapshot(self.root, "demo", lambda ids=ids: ids)
            self.assertEqual(result["pending_count"], len(ids))
            self.assertIsNone(result["pending_error"])

    def test_programming_and_memory_errors_are_not_masked(self):
        def broken():
            raise TypeError("programming error")
        with self.assertRaises(TypeError):
            solution.snapshot(self.root, "demo", broken)
        (self.root / "USER.md").unlink()
        with self.assertRaises(OSError):
            solution.snapshot(self.root, "demo", list)
