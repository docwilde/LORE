# SPDX-License-Identifier: AGPL-3.0-only
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("solution", "/workspace/benchmark.py")
solution = importlib.util.module_from_spec(spec)
spec.loader.exec_module(solution)


def record(content, role="user", **extra):
    return json.dumps({"type": role, "message": {"content": content}, **extra}) + "\n"


class WorkerChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "session.jsonl"

    def check(self, text, expected):
        self.path.write_text(text)
        self.assertEqual(solution.is_worker_transcript(self.path), expected)

    def test_large_preamble_and_both_markers(self):
        for marker in solution.MARKERS:
            self.check(record("x" * 80000, "system") + record(marker), True)

    def test_structured_text_and_assistant(self):
        self.check("not-json\n[]\nnull\n" + record([{"type": "text", "text": solution.MARKERS[1]}], "assistant"), True)

    def test_false_positives_in_metadata_thinking_and_bad_shapes(self):
        marker = solution.MARKERS[0]
        for text in (record("ordinary", cwd=marker), record(marker, "system"),
                     record([{"type": "thinking", "text": marker}]),
                     record({"text": marker}), json.dumps({"type": "user", "message": marker}) + "\n"):
            self.check(text, False)

    def test_record_bound_includes_malformed_records(self):
        marker = record(solution.MARKERS[0])
        self.check("broken\n" * (solution.MAX_RECORDS - 1) + marker, True)
        self.check("broken\n" * solution.MAX_RECORDS + marker, False)

    def test_byte_bound_and_missing_file(self):
        self.check(record("x" * solution.MAX_BYTES) + record(solution.MARKERS[0]), False)
        self.assertFalse(solution.is_worker_transcript(self.path / "missing"))

    def test_single_line_reads_are_bounded(self):
        class BoundedBytes(io.BytesIO):
            def read(self, size=-1):
                if size < 0 or size > solution.MAX_BYTES + 1:
                    raise AssertionError("unbounded read")
                return super().read(size)

            def readline(self, size=-1):
                if size < 0 or size > solution.MAX_BYTES + 1:
                    raise AssertionError("unbounded readline")
                return super().readline(size)

        data = b"x" * (solution.MAX_BYTES + 1024) + b"\n"
        with patch.object(Path, "open", return_value=BoundedBytes(data)):
            self.assertFalse(solution.is_worker_transcript(self.path))
