# SPDX-License-Identifier: AGPL-3.0-only
"""ISSUE #73: a synced skill lost its frontmatter description.

`pending._append_skill_op` put `{name, body}` on the wire with the RAW body,
while `pending.apply_item` wrote a `---\\nname: ...\\ndescription: "...
(lore-learned)"\\n---` wrapper to disk. The op carried no description, so a
receiver could not reproduce the author's `SKILL.md` byte for byte -- it
installed the bare body and the two files differed from the moment they
existed.

That is not a tidiness bug. `test_store_is_a_function_of_its_log` is the
property the whole sync design rests on: a store is what its op log says it
is. For skills it held only because the apply engine wrote the raw body,
matching what the op happened to carry -- the log described a file nobody had
authored. The fix makes the op carry what the author actually wrote.

THE ONE THAT MATTERS is test_synced_skill_is_byte_identical_to_the_authors_
file: machine A approves a skill, A's op crosses, machine B's SKILL.md is
compared to A's byte for byte. It fails on the pre-fix payload shape.

THE COMPATIBILITY HALF is test_the_shipped_signed_fixture_still_applies and
test_op_written_before_the_widening_still_installs_its_body. The fix WIDENS
`body` (bare body -> whole file) rather than adding a field or renaming one,
because every receiver of every version writes `body` to the skill file
verbatim: both readings land correctly, in both directions, with no version
flag. An op written before the change still applies, and still produces
exactly the file it always produced. The shipped protocol vector
(tests/fixtures/sync_protocol/skill_put.json, signed and unchanged) IS such an
op, so that compatibility is asserted against the real thing rather than
against a reconstruction of it.

The third group covers the consequence: the loser of a `put` conflict is
staged back as a pending proposal whose body is now a WHOLE FILE, so
`skill_file_text` has to be idempotent about the wrapper or approving that
proposal nests one frontmatter block inside another.

Isolated LORE_ROOT/LORE_SKILLS_DIR per machine in fresh temp dirs, never the
real ~/.claude/lore -- same convention as every other file in this suite.

Run: python3 tests/test_issue73_skill_roundtrip.py
"""

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN_LORE = REPO_ROOT / "bin" / "lore.py"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "sync_protocol"

# docs/sync-protocol.md Appendix B: public, fixed, and for these vectors only.
# Set before any module loads -- append_op reads it at call time, so every op
# authored here carries a real mac and the receiver has something to check.
TEST_HMAC_KEY = "lore-sync-protocol-test-key-DO-NOT-USE-IN-PRODUCTION"
os.environ["LORE_SYNC_HMAC_KEY"] = TEST_HMAC_KEY

MACHINE_A = "aaaaaaaa-7373-4373-8373-737373737373"
MACHINE_B = "bbbbbbbb-7373-4373-8373-737373737373"

# The shape every lore-learned skill on disk actually has (config.SKILL_NAME_RE).
SKILL_NAME = "worktree-agent-isolation"

# A description with an embedded double quote, because apply_item rewrites one
# to a single quote before it goes inside the frontmatter's quoted value. If
# the sender and the receiver disagreed about that rewrite the files would
# differ by exactly one byte -- the kind of difference a round trip is for.
SKILL_DESCRIPTION = 'Use real-disk worktrees for "concurrent" agents'
SKILL_BODY = (
    "Give each agent its own worktree.\n"
    "\n"
    "1. `git worktree add ../repo.worktrees/<name> -b <branch>`\n"
    "2. Squash-merge back.\n"
)


@contextlib.contextmanager
def quiet():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        yield buf


def _exec_lore(root: Path):
    """A fresh, isolated `lore` module bound to its OWN LORE_ROOT -- the
    test-isolation mechanism bin/lore.py's header documents. Two calls in one
    process give two INDEPENDENT lore_core instances, which is how one process
    plays two machines: the first exec's functions stay bound to the first
    instance's modules even after the second exec replaces sys.modules,
    because `from lore_core import *` binds names to objects at import time.
    """
    (root / "skills").mkdir(parents=True, exist_ok=True)
    (root / "projects_dir").mkdir(parents=True, exist_ok=True)
    os.environ["LORE_ROOT"] = str(root)
    os.environ["LORE_SKILLS_DIR"] = str(root / "skills")
    os.environ["LORE_PROJECTS_DIR"] = str(root / "projects_dir")
    spec = importlib.util.spec_from_file_location(f"lore_{uuid.uuid4().hex[:8]}", BIN_LORE)
    mod = importlib.util.module_from_spec(spec)
    with quiet():
        spec.loader.exec_module(mod)
    return mod


def _machine(label: str, machine_id: "str | None" = None):
    """(root, module) for one machine, with its identity pinned.

    LORE_MACHINE_ID is honoured only at FIRST creation, so the row is minted
    here, eagerly, while the variable still holds this machine's value."""
    root = Path(tempfile.mkdtemp(prefix=f"lore-test-issue73-{label}-"))
    if machine_id:
        os.environ["LORE_MACHINE_ID"] = machine_id
    else:
        os.environ.pop("LORE_MACHINE_ID", None)
    mod = _exec_lore(root)
    conn = mod.db_connect()
    mod.get_or_create_machine(conn)
    conn.commit()
    conn.close()
    return root, mod


def _skill_ops(mod) -> "list[dict]":
    """This machine's `skill` ops, wire-shaped -- what a push would send."""
    conn = mod.db_connect()
    rows = conn.execute(
        "SELECT op_id, machine_id, machine_seq, lamport, class, op, project_key,"
        " payload, mac, created FROM sync_ops WHERE class = 'skill' ORDER BY seq"
    ).fetchall()
    conn.close()
    return [
        {"op_id": r[0], "machine_id": r[1], "machine_seq": r[2], "lamport": r[3],
         "class": r[4], "op": r[5], "project_key": r[6], "payload": json.loads(r[7]),
         "mac": r[8], "created": r[9]}
        for r in rows
    ]


def _apply(mod, ops: "list[dict]") -> dict:
    conn = mod.db_connect()
    try:
        return mod.apply_ops(conn, ops)
    finally:
        conn.commit()
        conn.close()


def _skill_file(mod, name: str = SKILL_NAME) -> Path:
    return mod.SKILLS_DIR / name / "SKILL.md"


def _pending_items(root: Path) -> "dict[str, dict]":
    """{uid: item} for the pile that is still PENDING (archive excluded)."""
    out = {}
    pdir = root / "pending"
    if not pdir.exists():
        return out
    for f in pdir.glob("*.json"):
        item = json.loads(f.read_text(encoding="utf-8"))
        out[item.get("uid")] = item
    return out


def _author(mod, *, name=SKILL_NAME, description=SKILL_DESCRIPTION, body=SKILL_BODY,
            action=None):
    """Approve a skill proposal on `mod`, the way `lore approve` does."""
    item = {"kind": "skill", "name": name, "description": description, "body": body}
    if action:
        item["action"] = action
    with quiet():
        err = mod.apply_item(f"p-{uuid.uuid4().hex[:8]}", item, False)
    return err


# ---------------------------------------------------------------------------
# A) the round trip
# ---------------------------------------------------------------------------

class TestSkillRoundTrip(unittest.TestCase):

    def test_synced_skill_is_byte_identical_to_the_authors_file(self):
        """ISSUE #73, the whole of it: author writes a skill, the op crosses,
        the receiver reconstructs the SAME FILE.

        Byte-for-byte, not "contains the body" -- the defect was precisely
        that the body survived and everything around it did not."""
        _, a = _machine("author-a", MACHINE_A)
        self.assertIsNone(_author(a))
        authored = _skill_file(a).read_text(encoding="utf-8")

        ops = _skill_ops(a)
        self.assertEqual(len(ops), 1, "approving a skill must emit exactly one op")
        _, b = _machine("receiver-b", MACHINE_B)
        report = _apply(b, ops)
        self.assertEqual(report["applied"], 1, report)
        self.assertEqual(report["unverified"], 0, "signed under this suite's own key")

        received = _skill_file(b).read_text(encoding="utf-8")
        self.assertEqual(received, authored,
                         "the receiver's SKILL.md must be the author's, byte for byte")

    def test_the_description_crosses_the_wire(self):
        """The specific thing that was lost. Named separately from the byte
        comparison because a future change could keep the files identical by
        dropping the frontmatter on BOTH sides, and that would not be a fix."""
        _, a = _machine("desc-a", MACHINE_A)
        self.assertIsNone(_author(a))
        _, b = _machine("desc-b", MACHINE_B)
        _apply(b, _skill_ops(a))

        received = _skill_file(b).read_text(encoding="utf-8")
        # apply_item rewrites a double quote to a single one before it goes
        # inside the frontmatter's quoted value.
        self.assertIn('description: "Use real-disk worktrees for \'concurrent\' agents'
                      ' (lore-learned)"', received)
        self.assertIn(f"name: {SKILL_NAME}", received)
        self.assertIn("Give each agent its own worktree.", received)

    def test_the_op_payload_carries_the_whole_file(self):
        """docs/sync-protocol.md Appendix A and docs/plans/sync.md: `body` on a
        `skill`/`put` is the complete SKILL.md. Asserted on the payload itself,
        not only through a receiver, so the wire contract is pinned even if the
        apply engine's write path changes."""
        _, a = _machine("payload-a", MACHINE_A)
        self.assertIsNone(_author(a))
        payload = _skill_ops(a)[0]["payload"]

        self.assertEqual(payload["name"], SKILL_NAME)
        self.assertEqual(payload["body"], _skill_file(a).read_text(encoding="utf-8"))
        self.assertTrue(payload["body"].startswith("---\n"),
                        "the payload must carry the frontmatter, not the bare body")

    def test_a_retired_skill_still_crosses_as_a_bare_remove(self):
        """The widening touches `put` only. `remove` carries `{name}` and must
        not have grown a body along the way."""
        _, a = _machine("retire-a", MACHINE_A)
        self.assertIsNone(_author(a))
        with quiet():
            err = a.apply_item("r-1", {"kind": "skill", "action": "retire",
                                       "name": SKILL_NAME}, False)
        self.assertIsNone(err)

        ops = _skill_ops(a)
        self.assertEqual([o["op"] for o in ops], ["put", "remove"])
        self.assertEqual(ops[1]["payload"], {"name": SKILL_NAME})

        _, b = _machine("retire-b", MACHINE_B)
        _apply(b, ops)
        self.assertFalse(_skill_file(b).exists(),
                         "the remove must still uninstall on the receiver")


# ---------------------------------------------------------------------------
# B) an op written before the widening
# ---------------------------------------------------------------------------

class TestOlderOpsStillApply(unittest.TestCase):
    """`body` was WIDENED, not renamed and not replaced by a new field. These
    pin the reason that was the right move: an op from before the change is
    still applied, and still produces exactly the file it used to."""

    def test_the_shipped_signed_fixture_still_applies(self):
        """tests/fixtures/sync_protocol/skill_put.json is a pre-#73 op: a bare
        body, signed, published in docs/sync-protocol.md Appendix A. It is the
        real evidence for "an older op must still apply" -- unchanged by this
        fix, MAC intact, applied through the production engine."""
        vector = json.loads((FIXTURES / "skill_put.json").read_text(encoding="utf-8"))
        op = vector["op"]
        self.assertTrue(vector["mac_should_verify"])
        self.assertNotIn("---", op["payload"]["body"],
                         "the fixture is a pre-#73 op: a bare body, no frontmatter")

        _, b = _machine("fixture-b", MACHINE_B)
        report = _apply(b, [op])
        self.assertEqual(report["applied"], 1, report)
        self.assertEqual(report["unverified"], 0,
                         "the shipped MAC must still verify -- the payload is untouched")

        installed = _skill_file(b, op["payload"]["name"]).read_text(encoding="utf-8")
        self.assertEqual(installed, op["payload"]["body"],
                         "a pre-#73 op must still install exactly what it carries")

    def test_op_written_before_the_widening_still_installs_its_body(self):
        """The same property against a freshly minted old-shape op, so it is
        asserted for any name/body rather than only for the shipped vector."""
        _, b = _machine("legacy-b", MACHINE_B)
        legacy_body = "A bare body, exactly as pre-#73 senders put on the wire.\n"
        op = {
            "op_id": str(uuid.uuid4()), "machine_id": MACHINE_A, "machine_seq": 1,
            "lamport": 1, "class": "skill", "op": "put", "project_key": None,
            "payload": {"name": SKILL_NAME, "body": legacy_body},
            "created": "2026-01-01T00:00:00Z",
        }
        op["mac"] = b.compute_mac(op, TEST_HMAC_KEY)

        report = _apply(b, [op])
        self.assertEqual(report["applied"], 1, report)
        self.assertEqual(_skill_file(b).read_text(encoding="utf-8"), legacy_body)

    def test_approving_a_legacy_bare_body_still_gets_wrapped(self):
        """The other half of the same compatibility: a pre-#73 body reaching
        apply_item -- from an old pending pile, or staged by the apply engine
        from an old op -- must still be given the frontmatter wrapper. If the
        idempotency check were too eager it would install a SKILL.md with no
        frontmatter at all, which Claude Code does not load."""
        _, a = _machine("legacy-wrap-a", MACHINE_A)
        self.assertIsNone(_author(a, body="No frontmatter here.\n"))
        installed = _skill_file(a).read_text(encoding="utf-8")
        self.assertTrue(installed.startswith(f'---\nname: {SKILL_NAME}\n'))
        self.assertIn("No frontmatter here.", installed)


# ---------------------------------------------------------------------------
# C) the wrapper is idempotent -- the consequence of carrying a whole file
# ---------------------------------------------------------------------------

class TestWrapperIdempotence(unittest.TestCase):
    """sync_apply._apply_skill stages the LOSING body of a `put` conflict back
    as a pending proposal. That body is now a whole file, so approving it must
    not nest a second frontmatter block inside the first."""

    def test_approving_a_whole_file_proposal_does_not_nest_frontmatter(self):
        _, a = _machine("nest-a", MACHINE_A)
        whole_file = a.skill_file_text(SKILL_NAME, SKILL_DESCRIPTION, SKILL_BODY)
        self.assertIsNone(_author(a, description="a different description",
                                  body=whole_file))

        installed = _skill_file(a).read_text(encoding="utf-8")
        self.assertEqual(installed, whole_file,
                         "a body that is already a whole file must install as-is")
        self.assertEqual(installed.count("\n---\n"), 1,
                         "exactly one frontmatter block -- not one nested in another")
        self.assertNotIn("a different description", installed,
                         "the file's own frontmatter wins; the item's is not re-applied")

    def test_conflict_loser_reinstalls_byte_identically_when_approved(self):
        """End to end, the case the idempotence exists for: two machines put
        the same skill, the receiver installs the winner and stages the loser,
        and approving that staged proposal reproduces the LOSER'S file byte for
        byte -- not a re-wrapped mangling of it."""
        _, a = _machine("conflict-a", MACHINE_A)
        _, b = _machine("conflict-b", MACHINE_B)
        self.assertIsNone(_author(a, description="from machine A", body="A's body\n"))
        self.assertIsNone(_author(b, description="from machine B", body="B's body\n"))
        op_a, op_b = _skill_ops(a)[0], _skill_ops(b)[0]

        _, node = _machine("conflict-receiver")
        winner, loser = sorted([op_a, op_b], key=node.canonical_key)[::-1]
        _apply(node, [op_a])
        _apply(node, [op_b])
        self.assertEqual(_skill_file(node).read_text(encoding="utf-8"),
                         winner["payload"]["body"])

        staged = _pending_items(node.ROOT)
        item = next(i for i in staged.values() if i.get("kind") == "skill")
        self.assertEqual(item["body"], loser["payload"]["body"])
        with quiet():
            self.assertIsNone(node.apply_item(item["uid"], item, True))
        self.assertEqual(_skill_file(node).read_text(encoding="utf-8"),
                         loser["payload"]["body"],
                         "approving the loser must reproduce the loser's OWN file")


# ---------------------------------------------------------------------------
# D) the frontmatter test itself
# ---------------------------------------------------------------------------

class TestFrontmatterDetection(unittest.TestCase):
    """`skill_frontmatter` decides whether skill_file_text wraps. Too eager and
    a real body loses its frontmatter; too lax and a whole file gets a second
    one. Both directions are cheap to pin, so both are."""

    @classmethod
    def setUpClass(cls):
        _, cls.mod = _machine("frontmatter")

    def test_a_body_opening_with_a_horizontal_rule_is_not_frontmatter(self):
        """Markdown's `---` rule is the obvious false positive: a prose body
        may well open with one, and wrapping is exactly what it needs."""
        body = "---\n\nSome prose that opens with a rule.\n\n---\n\nMore prose.\n"
        self.assertFalse(self.mod.skill_frontmatter(body))
        wrapped = self.mod.skill_file_text(SKILL_NAME, "d", body)
        self.assertTrue(wrapped.startswith(f'---\nname: {SKILL_NAME}\n'))
        self.assertIn(body, wrapped)

    def test_an_unterminated_fence_is_not_frontmatter(self):
        self.assertFalse(self.mod.skill_frontmatter("---\nname: x\nno closing fence\n"))

    def test_an_empty_fence_pair_is_not_frontmatter(self):
        self.assertFalse(self.mod.skill_frontmatter("---\n---\n\nbody\n"))

    def test_a_real_wrapper_is_recognised(self):
        text = self.mod.skill_file_text(SKILL_NAME, SKILL_DESCRIPTION, SKILL_BODY)
        self.assertTrue(self.mod.skill_frontmatter(text))
        self.assertEqual(self.mod.skill_file_text(SKILL_NAME, "other", text), text,
                         "skill_file_text must be idempotent on its own output")


if __name__ == "__main__":
    unittest.main()
