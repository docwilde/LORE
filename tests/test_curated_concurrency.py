# SPDX-License-Identifier: AGPL-3.0-only
"""Concurrent curated writes must preserve every successful mutation."""

import multiprocessing
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path


os.environ["LORE_ROOT"] = tempfile.mkdtemp(prefix="lore-concurrent-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lore_core import filemap, gate, memory  # noqa: E402


def _write_memory(number, start):
    original = memory.read_entries

    def slow_read(path):
        entries = original(path)
        time.sleep(0.03)
        return entries

    memory.read_entries = slow_read
    start.wait()
    err = memory.memory_add("project", "race", f"entry-{number}")
    if err:
        raise AssertionError(err)


def _write_filemap(number, start):
    original = filemap.read_entries

    def slow_read(path):
        entries = original(path)
        time.sleep(0.03)
        return entries

    filemap.read_entries = slow_read
    start.wait()
    err = filemap.filemap_add("race", f"path-{number}", f"purpose-{number}")
    if err:
        raise AssertionError(err)


@unittest.skipUnless("fork" in multiprocessing.get_all_start_methods(),
                     "cross-process race test uses fork")
class CuratedConcurrency(unittest.TestCase):
    def _run_writers(self, target):
        ctx = multiprocessing.get_context("fork")
        start = ctx.Event()
        jobs = [ctx.Process(target=target, args=(i, start)) for i in range(8)]
        for job in jobs:
            job.start()
        start.set()
        for job in jobs:
            job.join(timeout=10)
            self.assertEqual(job.exitcode, 0)

    def test_simultaneous_memory_adds_keep_all_entries(self):
        self._run_writers(_write_memory)
        entries = memory.read_entries(memory.memory_path("project", "race"))
        self.assertEqual(set(entries), {f"entry-{i}" for i in range(8)})
        for entry in entries:
            self.assertTrue(gate.entry_provenance("memory", "project:race", entry))

    def test_simultaneous_filemap_adds_keep_all_rows(self):
        self._run_writers(_write_filemap)
        entries = filemap.read_entries(filemap.filemap_path("race"))
        self.assertEqual(set(entries),
                         {f"path-{i} — purpose-{i}" for i in range(8)})
        for entry in entries:
            self.assertTrue(gate.entry_provenance("filemap", "race", entry))


if __name__ == "__main__":
    unittest.main()
