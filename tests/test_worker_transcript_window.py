# SPDX-License-Identifier: AGPL-3.0-only
"""Regression tests for is_worker_transcript's marker detection (fixed
2026-08-24): the old implementation read a fixed 65536-byte head and matched
WORKER_MARKERS in it. A large preamble ahead of the marker-bearing record
(a big injected snapshot, a long system block, a big first message) pushes
the marker past that window, misclassifying our own deriver/dreamer
transcript as a real user session -- counted as pending review, reported as
waiting, potentially reviewed by a later deriver digesting its own output.

The fix reads the transcript structurally: the first WORKER_TRANSCRIPT_MAX_RECORDS
JSONL records (not bytes), each tested for the marker via its parsed message
content, with WORKER_TRANSCRIPT_MAX_BYTES as a backstop against one
pathological line. Run: python3 -m pytest tests/test_worker_transcript_window.py
"""

import importlib.util
import json
import os
import tempfile
import time
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="lore-test-")
os.environ["LORE_ROOT"] = os.path.join(TMP, "root")
os.environ["LORE_SKILLS_DIR"] = os.path.join(TMP, "skills")
os.environ["LORE_PROJECTS_DIR"] = os.path.join(TMP, "projects")

_spec = importlib.util.spec_from_file_location(
    "lore", Path(__file__).resolve().parent.parent / "bin" / "lore.py")
lore = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lore)


def _msg(role: str, text: str) -> str:
    return json.dumps({"type": role, "timestamp": "2026-08-24T00:00:00Z",
                       "cwd": "/tmp/proj", "message": {"content": text}})


def _write(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_marker_within_old_64kb_window_still_detected(tmp_path):
    # baseline: the un-padded, small case must keep working.
    t = tmp_path / "small-worker.jsonl"
    _write(t, [_msg("user", lore.WORKER_MARKERS[0] + " -- rest of the review prompt")])
    assert lore.is_worker_transcript(t) is True


def test_marker_beyond_64kb_is_still_detected():
    # THE FIX: pad the transcript with a filler record ahead of the marker
    # so the marker-bearing record starts well past byte 65536 -- exactly the
    # "large injected snapshot" / "long system block" scenario described in
    # the defect. Old code (fh.read(65536)) never even reaches this record.
    t = Path(tempfile.mkdtemp(prefix="lore-worker-")) / "padded-worker.jsonl"
    filler = "x" * 70_000  # alone already exceeds the old 64KB window
    lines = [
        _msg("user", filler),
        _msg("user", lore.WORKER_MARKERS[0] + " -- the actual review prompt"),
    ]
    _write(t, lines)
    assert os.path.getsize(t) > 65536
    assert lore.is_worker_transcript(t) is True


def test_second_marker_beyond_64kb_is_also_detected():
    t = Path(tempfile.mkdtemp(prefix="lore-worker-")) / "padded-dreamer.jsonl"
    filler = "y" * 80_000
    lines = [
        _msg("user", filler),
        _msg("user", lore.WORKER_MARKERS[1]),
    ]
    _write(t, lines)
    assert lore.is_worker_transcript(t) is True


def test_real_session_without_marker_is_never_misclassified():
    t = Path(tempfile.mkdtemp(prefix="lore-worker-")) / "real-session.jsonl"
    lines = [_msg("user" if i % 2 == 0 else "assistant", f"ordinary message {i}")
             for i in range(10)]
    _write(t, lines)
    assert lore.is_worker_transcript(t) is False


def test_large_real_session_stays_false_and_reads_bounded_data():
    # PERFORMANCE PROPERTY (preserved from the original docstring): "a real
    # session's transcript can be tens of megabytes" -- reading must stay
    # cheap regardless of file size. Build a file well past the record/byte
    # caps with no marker anywhere and assert both the correct verdict and a
    # generously-bounded completion time (an accidental return to whole-file
    # reads would blow well past this).
    t = Path(tempfile.mkdtemp(prefix="lore-worker-")) / "huge-real-session.jsonl"
    lines = [_msg("user" if i % 2 == 0 else "assistant", "y" * 5000 + f" msg {i}")
             for i in range(2000)]  # ~10MB, far more than WORKER_TRANSCRIPT_MAX_RECORDS
    _write(t, lines)
    assert os.path.getsize(t) > 5_000_000
    start = time.monotonic()
    result = lore.is_worker_transcript(t)
    elapsed = time.monotonic() - start
    assert result is False
    assert elapsed < 2.0, f"is_worker_transcript took {elapsed:.2f}s on a bounded read"


def test_marker_past_byte_backstop_is_not_detected():
    # the byte ceiling is a deliberate backstop, not just the record count:
    # a marker sitting past WORKER_TRANSCRIPT_MAX_BYTES worth of preamble is
    # correctly NOT found (it is outside both bounds) -- documents the
    # boundary rather than claiming unbounded detection.
    t = Path(tempfile.mkdtemp(prefix="lore-worker-")) / "past-backstop.jsonl"
    filler = "z" * (lore.WORKER_TRANSCRIPT_MAX_BYTES + 10_000)
    lines = [_msg("user", filler), _msg("user", lore.WORKER_MARKERS[0])]
    _write(t, lines)
    assert lore.is_worker_transcript(t) is False


def test_missing_file_returns_false():
    assert lore.is_worker_transcript(Path("/nonexistent/path/to/nothing.jsonl")) is False


def test_backfill_available_excludes_padded_worker_transcript():
    # end-to-end: cmd_backfill's --list builds `available` via
    # is_worker_transcript per file -- confirm the padded worker transcript
    # is excluded from what gets offered for review.
    slug = "-tmp-backfill-padded"
    proj_dir = lore.PROJECTS_DIR / slug
    filler = "w" * 70_000
    _write(proj_dir / "worker-padded.jsonl",
           [_msg("user", filler), _msg("user", lore.WORKER_MARKERS[0])])
    _write(proj_dir / "real.jsonl",
           [_msg("user", "hi"), _msg("assistant", "hello")])
    kept = [p.name for p in sorted(proj_dir.glob("*.jsonl"))
            if not lore.is_worker_transcript(p)]
    assert kept == ["real.jsonl"]
