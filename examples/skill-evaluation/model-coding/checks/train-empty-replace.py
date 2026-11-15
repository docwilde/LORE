# SPDX-License-Identifier: AGPL-3.0-only
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("solution", "/workspace/benchmark.py")
solution = importlib.util.module_from_spec(spec)
spec.loader.exec_module(solution)


class ReplacementChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "MEMORY.md").write_text("- Zebra fact\n- Keep scoped facts\n")
        (self.root / "provenance.json").write_text('{"Zebra fact":"approved","Keep scoped facts":"direct"}')
        (self.root / "ops.jsonl").write_text('{"op":"prior"}\n')

    def state(self):
        return {path.name: path.read_bytes() for path in self.root.iterdir()}

    def test_empty_normalization_is_transaction_free(self):
        before = self.state()
        for text in ("", " ", "\n\t\r", "\u00a0\u2003\n"):
            with self.subTest(text=repr(text)):
                self.assertEqual(solution.memory_replace(self.root, "scoped", text), "empty text")
                self.assertEqual(self.state(), before)

    def test_empty_is_rejected_before_any_read(self):
        with patch.object(Path, "read_text", side_effect=AssertionError("filesystem read")):
            self.assertEqual(solution.memory_replace(self.root / "missing", "absent", "\n"), "empty text")

    def test_success_preserves_provenance_and_records_normalized_text_once(self):
        self.assertIsNone(solution.memory_replace(self.root, "SCOPED", " Alpha\n beta "))
        self.assertEqual((self.root / "MEMORY.md").read_text(), "- Alpha beta\n- Zebra fact\n")
        self.assertEqual(json.loads((self.root / "provenance.json").read_text()),
                         {"Alpha beta": "direct", "Zebra fact": "approved"})
        ops = [json.loads(row) for row in (self.root / "ops.jsonl").read_text().splitlines()]
        self.assertEqual(ops, [{"op": "prior"}, {"op": "replace", "old": "Keep scoped facts", "text": "Alpha beta"}])

    def test_match_errors_and_cap_are_unchanged(self):
        before = self.state()
        for needle, text, options, expected in (
            ("missing", "new fact", {}, "match_not_found"),
            ("fact", "new fact", {}, "match_ambiguous"),
            ("keep scoped facts", "new fact", {"exact": True}, "match_not_found"),
            ("scoped", "x" * 200, {"cap": 40}, "over_cap")):
            self.assertEqual(solution.memory_replace(self.root, needle, text, **options), expected)
            self.assertEqual(self.state(), before)

    def test_exact_success(self):
        self.assertIsNone(solution.memory_replace(self.root, "Keep scoped facts", "Preserve Unicode \u03bb", exact=True))
        self.assertIn("Preserve Unicode \u03bb", (self.root / "MEMORY.md").read_text())
