# SPDX-License-Identifier: AGPL-3.0-only
"""Receiver lookup uses project identity, not the author's checkout path.

Run: python3 tests/test_sync_project_keys.py
"""
import unittest

from test_sync_merge import (
    MACHINE_A, MACHINE_B, _align_project, _apply, _entries, _machine,
    _map_entries, _read_ops, _signed, quiet,
)


class PortableProjectEntryKeys(unittest.TestCase):
    def test_project_replace_remove_and_exact_keys_survive_different_slugs(self):
        _, author = _machine("portable-author", MACHINE_A)
        slug = "author-checkout"
        author.memory_add("project", slug, "shared fact")
        author.memory_add("project", slug, "shared fact extended")
        author.filemap_add(slug, "src.rs", "shared purpose")
        author.filemap_add(slug, "src.rs.backup", "shared purpose extended")
        seed = _read_ops(author)
        project_key = seed[0]["project_key"]
        author.memory_replace("project", slug, "shared fact", "rewritten fact", exact=True)
        author.filemap_replace(slug, "src.rs — shared purpose", "src.rs", "rewritten purpose", exact=True)
        author.memory_remove("project", slug, "shared fact extended", exact=True)
        author.filemap_remove(slug, "src.rs.backup — shared purpose extended", exact=True)
        ops = _read_ops(author)
        for label, mapped_slug in (("mapped", "receiver-checkout"), ("synthetic", None)):
            with self.subTest(receiver=label):
                _, receiver = _machine("portable-" + label, MACHINE_B)
                if mapped_slug:
                    _align_project(receiver, project_key, mapped_slug)
                self.assertEqual(_apply(receiver, seed)["applied"], len(seed))
                report = _apply(receiver, ops)
                self.assertEqual(report["failed"], 0, report)
                with receiver.db_connect() as conn:
                    local_slug = conn.execute("SELECT slug FROM sync_projects WHERE project_key=?", (project_key,)).fetchone()[0]
                self.assertNotEqual(local_slug, slug)
                self.assertEqual(_entries(receiver, "project", local_slug), ["rewritten fact"])
                self.assertEqual(_map_entries(receiver, local_slug), ["src.rs — rewritten purpose"])
                self.assertEqual({o["op_id"]: o for o in _read_ops(receiver)},
                                 {o["op_id"]: o for o in ops}, "translation must preserve signed rows and never echo")
                self.assertEqual(_apply(receiver, ops)["duplicate"], len(ops))

    def test_concurrent_same_path_rewrites_keep_both_and_resolve_exactly(self):
        _, a = _machine("portable-conflict-a", MACHINE_A)
        a.filemap_add("author-path", "src.rs", "original purpose")
        seed = _read_ops(a)
        project_key = seed[0]["project_key"]
        _, b = _machine("portable-conflict-b", MACHINE_B)
        _align_project(b, project_key, "receiver-path")
        _apply(b, seed)
        a.filemap_replace("author-path", "original purpose", "src.rs", "A purpose")
        b.filemap_replace("receiver-path", "original purpose", "src.rs", "B purpose")
        a_ops, b_ops = _read_ops(a), _read_ops(b)
        _apply(a, b_ops)
        _apply(b, a_ops)
        expected = ["src.rs — A purpose", "src.rs — B purpose"]
        for mod, slug in ((a, "author-path"), (b, "receiver-path")):
            self.assertEqual(_map_entries(mod, slug), expected)
            with mod.db_connect() as conn:
                pairs = mod.conflict_rows(conn)
            self.assertEqual(len(pairs), 1)
            self.assertEqual(set(pairs[0][2:4]), set(expected))
        # A human removes one wording. The sync key must not remove both rows
        # sharing the same path, or fail because the path match is ambiguous.
        a.filemap_remove("author-path", "B purpose")
        _apply(b, _read_ops(a))
        self.assertEqual(_map_entries(b, "receiver-path"), ["src.rs — A purpose"])
        with b.db_connect() as conn:
            self.assertEqual(b.conflict_rows(conn), [])

    def test_replaces_compare_project_identity_and_translate_both_authors(self):
        _, receiver = _machine("portable-project-conflicts")
        old = receiver.entry_key("memory", "project:sender-a", "original fact")
        other_old = receiver.entry_key("memory", "project:sender-b", "original fact")
        ops = [
            _signed(receiver, machine_id=MACHINE_A, machine_seq=1, lamport=1,
                    cls="memory", verb="replace", project_key="project-one",
                    payload={"old_key": old, "text": "first project wording"}),
            _signed(receiver, machine_id=MACHINE_B, machine_seq=1, lamport=2,
                    cls="memory", verb="replace", project_key="project-two",
                    payload={"old_key": other_old, "text": "second project wording"}),
        ]
        self.assertEqual(_apply(receiver, ops)["failed"], 0)
        with receiver.db_connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM sync_conflicts").fetchone()[0], 0)
        competing = _signed(receiver, machine_id=MACHINE_B, machine_seq=2, lamport=3,
                            cls="memory", verb="replace", project_key="project-one",
                            payload={"old_key": other_old, "text": "competing first project wording"})
        self.assertEqual(_apply(receiver, [competing])["applied"], 1)
        with receiver.db_connect() as conn:
            pairs = receiver.conflict_rows(conn)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(set(pairs[0][2:4]), {"first project wording", "competing first project wording"})

    def test_wrong_scopes_and_malformed_keys_cannot_target_user_memory(self):
        _, receiver = _machine("portable-invalid-key")
        receiver.memory_add("user", "", "untouched fact")
        keys = [receiver.entry_key("memory", "project:sender", "untouched fact"),
                receiver.entry_key("memory", "machine:sender", "untouched fact"),
                receiver.entry_key("filemap", "sender", "untouched fact"),
                "memory:user:malformed", None]
        for seq, key in enumerate(keys, 1):
            op = _signed(receiver, machine_id=MACHINE_A, machine_seq=seq, lamport=seq,
                         cls="memory", verb="remove", payload={"key": key})
            with quiet():
                report = _apply(receiver, [op])
            self.assertEqual(report["failed"], 1, report)
            self.assertEqual(_entries(receiver), ["untouched fact"])


if __name__ == "__main__":
    unittest.main()
