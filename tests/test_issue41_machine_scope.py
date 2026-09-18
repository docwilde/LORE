# SPDX-License-Identifier: AGPL-3.0-only
"""ISSUE #41: machine facts had no scope, so they defaulted into user memory.

The scope vocabulary was `user | project`. A hardware quirk, a driver
workaround, a kernel or sandbox capability, a tmpfs size, where a tool
happens to be installed on THIS box -- none of those is a fact about the
person and none is a fact about the repo, so they all landed in user memory,
which is a portable claim about who the user is and is injected on every
machine they work on. A laptop's Wi-Fi workaround was being asserted on the
workstation with the authority of a preference.

THE ONE THAT MATTERS is test_another_hosts_fact_is_never_injected_here: that
is the bug, stated as a property. The rest of section B is the injection
policy that makes it hold, and section C is the same property across the
sync boundary, which is where it stopped being a tidiness problem: since
0.55.0 memory MOVES between machines, so a fact filed in the wrong scope does
not merely sit there being wrong, it travels.

SECTION C IS THE ONE TO READ FIRST IF YOU ARE CHANGING THE WIRE. Machine
memory deliberately emits no `memory` op. The wire has no scope field for
that class -- `project_key is null` IS user scope, and a receiver's only
scope switch is that two-way test -- so a machine op would travel as null and
be filed into USER.md on every machine that received it, which is the bug
with a courier. test_a_machine_write_emits_no_memory_op is the guard on that,
and it is the test to look at if someone later "fixes" the missing op.

SECTION D is the migration, and its bar is that nothing is lost: existing
stores have machine facts sitting in user memory right now, every refusal
path leaves both sides untouched, and nothing is reclassified without being
named.

Isolated LORE_ROOT/LORE_SKILLS_DIR in fresh temp dirs, never the real
~/.claude/lore -- same convention as every other file in this suite. The
machine-crossing tests exec a SECOND, independent lore instance per machine,
the mechanism bin/lore.py's header documents.

Run: python3 tests/test_issue41_machine_scope.py
"""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest
import uuid
from argparse import Namespace
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN_LORE = REPO_ROOT / "bin" / "lore.py"

# docs/sync-protocol.md Appendix B: public, fixed, and for these vectors only.
# Set before any module loads so the ops this file authors carry a real mac.
TEST_HMAC_KEY = "lore-sync-protocol-test-key-DO-NOT-USE-IN-PRODUCTION"
os.environ["LORE_SYNC_HMAC_KEY"] = TEST_HMAC_KEY

# All state dirs must point away from the real store BEFORE the module
# executes: lore.py reads them at import time into module constants. The
# machine cap is pinned small so the over-cap path is testable with a
# handful of rows.
TMP = tempfile.mkdtemp(prefix="lore-test-issue41-")
os.environ["LORE_ROOT"] = os.path.join(TMP, "root")
os.environ["LORE_SKILLS_DIR"] = os.path.join(TMP, "skills")
os.environ["LORE_PROJECTS_DIR"] = os.path.join(TMP, "projects")
os.environ["LORE_MACHINE_CAP"] = "400"
os.environ["LORE_MACHINE_HOST"] = "laptop"

_spec = importlib.util.spec_from_file_location("lore", BIN_LORE)
lore = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lore)

HOST = "laptop"
OTHER = "workstation"

# The issue's own examples, so the tests are about the facts it is about.
WIFI = "wifi driver needs the 550 series pinned or the link drops under load"
TMPFS = "/tmp here is a 16 GB tmpfs — a decompress there OOMs the box"
PREFERENCE = "prefers concise, declarative commit messages"


@contextlib.contextmanager
def quiet():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        yield buf


@contextlib.contextmanager
def as_host(name: str):
    """Run this block as though lore were on host `name`. this_machine() reads
    the environment per call precisely so a test can do this."""
    before = os.environ.get("LORE_MACHINE_HOST")
    os.environ["LORE_MACHINE_HOST"] = name
    try:
        yield name
    finally:
        if before is None:
            os.environ.pop("LORE_MACHINE_HOST", None)
        else:
            os.environ["LORE_MACHINE_HOST"] = before


def _wipe():
    """Reset the base instance's state between tests. The scope directories go
    entirely, not just their contents: a store that has never filed a machine
    fact has no ROOT/machines at all, and that is the state most existing
    stores are in."""
    root = Path(lore.ROOT)
    for rel in ("USER.md", "provenance.json", "state.db"):
        (root / rel).unlink(missing_ok=True)
    for sub in ("machines", "projects", "pending"):
        shutil.rmtree(root / sub, ignore_errors=True)


def _entries(scope: str, key: str = "") -> "list[str]":
    return lore.read_entries(lore.memory_path(scope, key))


# ---------------------------------------------------------------------------
# A) the scope exists and is its own store
# ---------------------------------------------------------------------------

class TestMachineScopeIsItsOwnStore(unittest.TestCase):

    def setUp(self):
        _wipe()

    def test_a_machine_fact_does_not_land_in_user_memory(self):
        """The defect in one line: this used to have nowhere to go but
        USER.md, where it is asserted on every machine."""
        self.assertIsNone(lore.memory_add("machine", HOST, WIFI))
        self.assertEqual(_entries("machine", HOST), [WIFI])
        self.assertEqual(_entries("user"), [],
                         "a machine fact must not touch user memory")

    def test_each_host_gets_its_own_file(self):
        lore.memory_add("machine", HOST, WIFI)
        lore.memory_add("machine", OTHER, TMPFS)
        self.assertEqual(_entries("machine", HOST), [WIFI])
        self.assertEqual(_entries("machine", OTHER), [TMPFS])
        self.assertEqual(sorted(lore.known_machines()), [HOST, OTHER])

    def test_machine_scope_has_its_own_cap_not_the_user_one(self):
        """The issue's cap-pressure half: machine facts stop competing with
        preferences for the user cap. LORE_MACHINE_CAP is pinned to 400 here,
        so a value borrowed from USER_CAP (9000) would not refuse."""
        self.assertEqual(lore.memory_cap("machine"), lore.MACHINE_CAP)
        self.assertNotEqual(lore.MACHINE_CAP, lore.USER_CAP)
        err = lore.memory_add("machine", HOST, "A" * 500)
        self.assertIsNotNone(err)
        self.assertIn("OVER CAP", err)
        self.assertEqual(_entries("machine", HOST), [],
                         "over cap must write nothing, not truncate")

    def test_provenance_is_tracked_per_host_not_per_scope(self):
        """Two boxes' notes are two facts. A shared bucket would let a
        `forget` on one host quietly drop the other's provenance row."""
        self.assertEqual(lore.memory_bucket("machine", HOST), f"machine:{HOST}")
        self.assertNotEqual(lore.memory_bucket("machine", HOST),
                            lore.memory_bucket("machine", OTHER))
        lore.memory_add("machine", HOST, WIFI)
        self.assertIn("provenance", lore.provenance_tag(
            "memory", lore.memory_bucket("machine", HOST), [WIFI]))

    def test_a_store_that_never_files_a_machine_fact_still_works(self):
        """Every existing store is this store. known_machines() must not need
        the directory to exist, and nothing may read it into being."""
        self.assertEqual(lore.known_machines(), [])
        self.assertEqual(_entries("machine", HOST), [])
        self.assertFalse((Path(lore.ROOT) / "machines").exists(),
                         "reading must not create the directory")


# ---------------------------------------------------------------------------
# B) injection -- the correctness bug itself
# ---------------------------------------------------------------------------

class TestInjectionPolicy(unittest.TestCase):

    def setUp(self):
        _wipe()

    def test_another_hosts_fact_is_never_injected_here(self):
        """ISSUE #41, the whole of it. A Wi-Fi workaround for the laptop is
        simply FALSE on the workstation, and it used to be stated there with
        the same authority as a preference."""
        lore.memory_add("machine", HOST, WIFI)
        with as_host(OTHER):
            snapshot = lore.build_context(TMP, "all")
        self.assertNotIn(WIFI, snapshot,
                         "a fact about another box must not be asserted here")
        with as_host(HOST):
            snapshot = lore.build_context(TMP, "all")
        self.assertIn(WIFI, snapshot,
                      "and it must still be asserted on the box it is about")

    def test_other_hosts_cost_one_pointer_line_not_their_bodies(self):
        """The fleet must not cost context on every session -- the same
        pull-on-demand discipline the file map already keeps."""
        lore.memory_add("machine", OTHER, TMPFS)
        with as_host(HOST):
            snapshot = lore.build_context(TMP, "all")
        self.assertNotIn(TMPFS, snapshot)
        pointer = [ln for ln in snapshot.splitlines() if "Other machines on file" in ln]
        self.assertEqual(len(pointer), 1, snapshot)
        self.assertIn(OTHER, pointer[0])
        self.assertIn("lore memory show --scope machine --host", pointer[0])

    def test_the_injected_block_says_it_is_true_of_this_host_only(self):
        """The block is read by a model that has to decide whether a fact
        generalizes. Labelling it is the cheap half of not generalizing it."""
        lore.memory_add("machine", HOST, WIFI)
        with as_host(HOST):
            snapshot = lore.build_context(TMP, "all")
        self.assertIn("## Machine memory", snapshot)
        self.assertIn(f"— {HOST}", snapshot)
        self.assertIn("True of THIS host only", snapshot)

    def test_a_store_with_no_machine_memory_pays_nothing_for_the_tier(self):
        """Every store that predates this change. A new empty section in the
        snapshot would spend context on every session of every such store."""
        with as_host(HOST):
            snapshot = lore.build_context(TMP, "all")
        self.assertNotIn("Machine memory", snapshot)
        self.assertNotIn("Other machines on file", snapshot)

    def test_machine_is_a_view_scope_of_its_own(self):
        lore.memory_add("machine", HOST, WIFI)
        lore.memory_add("user", "", PREFERENCE)
        with as_host(HOST):
            self.assertIn(WIFI, lore.build_context(TMP, "machine"))
            self.assertNotIn(PREFERENCE, lore.build_context(TMP, "machine"))
            self.assertNotIn(WIFI, lore.build_context(TMP, "user"))
        self.assertIn("machine", lore.SCOPES)
        self.assertEqual(lore.effective_scope("machine"), "machine")


# ---------------------------------------------------------------------------
# C) sync -- a machine-scoped fact crossing to another machine
# ---------------------------------------------------------------------------

def _exec_lore(root: Path):
    """A fresh, isolated `lore` module bound to its OWN LORE_ROOT -- the
    test-isolation mechanism bin/lore.py's header documents. Two calls in one
    process give two INDEPENDENT lore_core instances, which is how one process
    plays two machines."""
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


def _machine(label: str, host: str):
    """(root, module) for one machine, pinned to a host name."""
    root = Path(tempfile.mkdtemp(prefix=f"lore-test-issue41-{label}-"))
    os.environ.pop("LORE_MACHINE_ID", None)
    os.environ["LORE_MACHINE_HOST"] = host
    mod = _exec_lore(root)
    conn = mod.db_connect()
    mod.get_or_create_machine(conn)
    conn.commit()
    conn.close()
    return root, mod


def _ops(mod, cls: "str | None" = None) -> "list[dict]":
    conn = mod.db_connect()
    sql = ("SELECT op_id, machine_id, machine_seq, lamport, class, op, project_key,"
           " payload, mac, created FROM sync_ops")
    rows = conn.execute(sql + (" WHERE class = ?" if cls else "") + " ORDER BY seq",
                        (cls,) if cls else ()).fetchall()
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


class TestMachineMemoryAndSync(unittest.TestCase):
    """The reason this stopped being a tidiness problem. Memory moves between
    machines as of 0.55.0, so a fact in the wrong scope now travels."""

    def setUp(self):
        _wipe()

    def test_a_machine_write_emits_no_memory_op(self):
        """THE GUARD. The wire has no scope field for the memory class:
        `project_key is null` IS user scope. A machine op would have to travel
        as null and be filed into USER.md on every receiver -- the original bug
        with a courier. If someone later "fixes" the missing op, this fails
        first, and test_a_machine_fact_never_reaches_another_machines_user_
        memory shows what it would cost."""
        lore.memory_add("user", "", PREFERENCE)
        lore.memory_add("machine", HOST, WIFI)
        payloads = [o["payload"].get("text") for o in _ops(lore, "memory")]
        self.assertIn(PREFERENCE, payloads, "a user write must still sync")
        self.assertNotIn(WIFI, payloads, "a machine write must not")

    def test_a_machine_fact_reaches_no_memory_file_on_another_machine(self):
        """End to end through the real apply engine: the laptop learns one
        fact of each kind, every op it produced crosses to the workstation,
        and only the portable one lands there.

        Asserted against EVERY memory file the receiver has, not just USER.md.
        A machine op has two possible wire shapes and both are wrong in
        different places -- `project_key: null` puts it in user memory, a
        resolved key hides it in a phantom project named after the host -- so
        a test that only watched USER.md would miss half of what it is for."""
        _, a = _machine("a", HOST)
        _, b = _machine("b", OTHER)
        a.memory_add("user", "", PREFERENCE)
        a.memory_add("machine", a.this_machine(), WIFI)

        report = _apply(b, _ops(a))
        self.assertEqual(report["unverified"], 0, report)

        os.environ["LORE_MACHINE_HOST"] = OTHER
        self.assertEqual(b.read_entries(b.memory_path("user", "")), [PREFERENCE])
        landed = sorted(p for p in Path(b.ROOT).rglob("*.md")
                        if WIFI in p.read_text(encoding="utf-8"))
        self.assertEqual(landed, [], "a machine fact must reach no store here")
        self.assertNotIn(WIFI, b.build_context(str(b.ROOT), "all"))
        self.assertEqual(b.known_machines(), [],
                         "nothing about the laptop should have been invented here")

    def test_a_machine_proposal_that_crosses_is_filed_under_the_host_it_names(self):
        """A staged proposal DOES cross -- `pending` ops carry the item as an
        opaque blob -- and that is fine as long as it names its host. Approved
        on the workstation, a laptop fact must land under `laptop`, not under
        the approver, and must not be injected there."""
        _, a = _machine("prop-a", HOST)
        _, b = _machine("prop-b", OTHER)
        os.environ["LORE_MACHINE_HOST"] = HOST
        review = {"memory": [{"scope": "machine", "action": "add", "text": WIFI}]}
        with quiet():
            self.assertEqual(a.stage_proposals(review, "-some-repo", "sess-x"), 1)
        stage_ops = [o for o in _ops(a, "pending") if o["op"] == "stage"]
        self.assertEqual(len(stage_ops), 1, "the proposal must have been staged")
        self.assertIsNone(stage_ops[0]["project_key"],
                          "a machine proposal has no project to be filed against")
        self.assertEqual(stage_ops[0]["payload"]["item"]["host"], HOST,
                         "the host must ride the wire inside the item")

        _apply(b, _ops(a))
        os.environ["LORE_MACHINE_HOST"] = OTHER
        staged = json.loads(next(iter(sorted((b.ROOT / "pending").glob("*.json"))))
                            .read_text(encoding="utf-8"))
        with quiet():
            self.assertIsNone(b.apply_item(staged["uid"], staged, False))
        self.assertEqual(b.read_entries(b.memory_path("machine", HOST)), [WIFI])
        self.assertEqual(b.read_entries(b.memory_path("machine", OTHER)), [],
                         "it is not a fact about the machine that approved it")
        self.assertEqual(b.read_entries(b.memory_path("user", "")), [])
        self.assertNotIn(WIFI, b.build_context(str(b.ROOT), "all"))


# ---------------------------------------------------------------------------
# D) migration -- existing stores already hold these facts in user memory
# ---------------------------------------------------------------------------

class TestMigration(unittest.TestCase):

    def setUp(self):
        _wipe()

    def test_a_user_entry_moves_into_machine_memory_unchanged(self):
        lore.memory_add("user", "", TMPFS)
        self.assertIsNone(lore.memory_move("user", "", "16 GB tmpfs", HOST,
                                           to_scope="machine"))
        self.assertEqual(_entries("user"), [])
        self.assertEqual(_entries("machine", HOST), [TMPFS],
                         "the text must survive the move byte for byte")

    def test_nothing_is_reclassified_without_being_named(self):
        """The issue asks for this explicitly and it is the right call: a
        sweep guessing which user entries are "about this box" would be a
        model deleting from the user's memory unsupervised."""
        lore.memory_add("user", "", WIFI)
        lore.memory_add("user", "", PREFERENCE)
        with as_host(HOST):
            lore.build_context(TMP, "all")
        self.assertEqual(sorted(_entries("user")), sorted([WIFI, PREFERENCE]),
                         "reading the store must never re-file anything")
        self.assertEqual(lore.known_machines(), [])

    def test_the_removal_propagates_so_the_fact_stops_being_asserted_elsewhere(self):
        """The migration's whole point. The fact reached the other machines
        through a synced `add`; if only the local re-filing happened it would
        be right here and still wrong everywhere else."""
        _, a = _machine("mig-a", HOST)
        _, b = _machine("mig-b", OTHER)
        a.memory_add("user", "", TMPFS)
        _apply(b, _ops(a))
        self.assertEqual(b.read_entries(b.memory_path("user", "")), [TMPFS])

        os.environ["LORE_MACHINE_HOST"] = HOST
        self.assertIsNone(a.memory_move("user", "", "16 GB tmpfs", HOST,
                                        to_scope="machine"))
        _apply(b, _ops(a))
        self.assertEqual(b.read_entries(b.memory_path("user", "")), [],
                         "the removal must reach the machine it was wrong on")
        self.assertEqual(b.read_entries(b.memory_path("machine", HOST)), [],
                         "and the arrival must not, since it is not true there")
        self.assertEqual(a.read_entries(a.memory_path("machine", HOST)), [TMPFS])

    def test_an_over_cap_destination_refuses_and_loses_nothing(self):
        lore.memory_add("user", "", TMPFS)
        lore.write_entries(lore.memory_path("machine", HOST), ["A" * 380],
                           lore.MACHINE_CAP, "machine")
        before_dst = _entries("machine", HOST)
        err = lore.memory_move("user", "", "16 GB tmpfs", HOST, to_scope="machine")
        self.assertIsNotNone(err)
        self.assertIn("OVER CAP", err)
        self.assertEqual(_entries("user"), [TMPFS], "the source must be intact")
        self.assertEqual(_entries("machine", HOST), before_dst)

    def test_a_non_matching_match_refuses_and_loses_nothing(self):
        lore.memory_add("user", "", TMPFS)
        err = lore.memory_move("user", "", "no such entry", HOST, to_scope="machine")
        self.assertIn("no entry matches", err)
        self.assertEqual(_entries("user"), [TMPFS])
        self.assertEqual(_entries("machine", HOST), [])

    def test_an_ambiguous_match_refuses_and_loses_nothing(self):
        lore.memory_add("user", "", "alpha driver quirk one")
        lore.memory_add("user", "", "alpha driver quirk two")
        err = lore.memory_move("user", "", "alpha driver quirk", HOST,
                               to_scope="machine")
        self.assertIn("ambiguous", err)
        self.assertEqual(len(_entries("user")), 2)
        self.assertEqual(_entries("machine", HOST), [])

    def test_provenance_travels_with_the_migrated_entry(self):
        """ISSUE #43: re-filing a fact does not turn an approved entry into a
        freshly written one."""
        lore.memory_add("user", "", TMPFS, via="approved")
        lore.memory_move("user", "", "16 GB tmpfs", HOST, to_scope="machine")
        prov = lore.entry_provenance("memory", lore.memory_bucket("machine", HOST), TMPFS)
        self.assertEqual(prov.get("via"), "approved")
        self.assertEqual(prov.get("origin"), "moved from user")

    def test_a_same_scope_user_move_is_still_refused_and_says_where_to_go(self):
        err = lore.memory_move("user", "a", "x", "b")
        self.assertIn("only project-scoped", err)
        self.assertIn("--to-machine", err, "the refusal must name the migration")

    def test_a_machine_entry_can_be_re_filed_onto_another_host(self):
        lore.memory_add("machine", HOST, TMPFS)
        self.assertIsNone(lore.memory_move("machine", HOST, "16 GB tmpfs", OTHER))
        self.assertEqual(_entries("machine", HOST), [])
        self.assertEqual(_entries("machine", OTHER), [TMPFS])
        self.assertIn("same machine",
                      lore.memory_move("machine", HOST, "x", HOST) or "")


# ---------------------------------------------------------------------------
# E) naming a host
# ---------------------------------------------------------------------------

class TestHostResolution(unittest.TestCase):

    def setUp(self):
        _wipe()

    def test_an_unknown_host_is_minted_rather_than_refused(self):
        """The asymmetry with resolve_subject_slug is deliberate: the fleet
        case is precisely a box lore has never run on, so refusing an
        unrecognised host would refuse the thing the scope is for."""
        self.assertEqual(lore.resolve_machine_key("gpu-box"), "gpu-box")
        self.assertIsNone(lore.resolve_subject_slug("gpu-box"),
                          "a project, by contrast, must already exist")

    def test_an_unambiguous_substring_resolves_to_a_known_host(self):
        lore.memory_add("machine", "workstation", TMPFS)
        self.assertEqual(lore.resolve_machine_key("works"), "workstation")
        self.assertEqual(lore.resolve_machine_key("station"), "workstation")

    def test_host_names_are_slugged_and_case_folded(self):
        self.assertEqual(lore.machine_slug("Work Station.local"), "work-station-local")
        self.assertEqual(lore.machine_slug("  GPU_BOX  "), "gpu-box")
        self.assertEqual(lore.machine_slug("../escape"), "escape",
                         "a host name must not be able to leave ROOT/machines")

    def test_a_slugged_host_stays_inside_the_machines_directory(self):
        key = lore.resolve_machine_key("../../etc/passwd")
        path = lore.memory_path("machine", key).resolve()
        self.assertEqual(path.parent, (Path(lore.ROOT) / "machines").resolve())

    def test_naming_no_host_means_this_host(self):
        with as_host("some-box"):
            self.assertEqual(lore.resolve_machine_key(None), "some-box")
            self.assertEqual(lore.resolve_machine_key(""), "some-box")
            self.assertEqual(lore.this_machine(), "some-box")


# ---------------------------------------------------------------------------
# F) the CLI and the deriver
# ---------------------------------------------------------------------------

class TestCommandLine(unittest.TestCase):

    def setUp(self):
        _wipe()

    def test_add_and_show_route_through_the_host_flag(self):
        with quiet() as out:
            rc = lore.cmd_memory(Namespace(mcmd="add", scope="machine", cwd=None,
                                           host="gpu-box", match=None, text=[TMPFS]))
        self.assertEqual(rc, 0)
        self.assertIn("gpu-box", out.getvalue())
        self.assertEqual(_entries("machine", "gpu-box"), [TMPFS])
        with quiet() as out, as_host(HOST):
            self.assertEqual(lore.cmd_memory(
                Namespace(mcmd="show", scope=None, cwd=None, host=None)), 0)
        shown = out.getvalue()
        self.assertIn("## machine — laptop", shown)
        self.assertIn("other machines on file: gpu-box", shown)
        self.assertNotIn(TMPFS, shown, "another host's body stays pull-on-demand")

    def test_the_migration_runs_from_the_command_line(self):
        lore.memory_add("user", "", TMPFS)
        with quiet() as out, as_host(HOST):
            rc = lore.cmd_memory(Namespace(mcmd="move", scope="user", cwd=None,
                                           host=None, match="16 GB tmpfs",
                                           to=None, to_machine=HOST))
        self.assertEqual(rc, 0, out.getvalue())
        self.assertEqual(_entries("user"), [])
        self.assertEqual(_entries("machine", HOST), [TMPFS])

    def test_a_pre_existing_move_invocation_still_works(self):
        """A Namespace built before --host/--to-machine existed carries
        neither attribute. cmd_memory must read them defensively or every
        older caller (and #40's own tests) breaks."""
        with quiet() as out:
            rc = lore.cmd_memory(Namespace(mcmd="move", scope="project", cwd=None,
                                           match="x", to="no-such-project"))
        self.assertEqual(rc, 1)
        self.assertIn("cannot resolve destination", out.getvalue())

    def test_argparse_accepts_the_new_flags_and_rejects_two_destinations(self):
        parser_ok = mock.patch.object(lore.sys, "argv",
                                      ["lore", "memory", "add", "--scope", "machine",
                                       "--host", "gpu-box", "hello"])
        with parser_ok, quiet():
            self.assertEqual(lore.main(), 0)
        self.assertEqual(_entries("machine", "gpu-box"), ["hello"])
        with mock.patch.object(lore.sys, "argv",
                               ["lore", "memory", "move", "--scope", "user",
                                "--match", "x", "--to", "p", "--to-machine", "h"]), \
                quiet(), self.assertRaises(SystemExit):
            lore.main()


class TestDeriverScope(unittest.TestCase):

    def setUp(self):
        _wipe()

    def test_the_deriver_may_propose_machine_scope_and_it_names_this_host(self):
        """A machine fact derived from a session is about the box that session
        ran on. The deriver is given no way to name a different host: letting
        a model attribute a quirk to a box it has never seen is a guess filed
        as a fact."""
        review = {"memory": [{"scope": "machine", "action": "add", "text": WIFI}]}
        with quiet(), as_host(HOST):
            n = lore.stage_proposals(review, "-a-repo", "sess-1")
        self.assertEqual(n, 1, "a machine-scoped proposal must not be malformed")
        items = [json.loads(p.read_text(encoding="utf-8"))
                 for p in (Path(lore.ROOT) / "pending").glob("*.json")]
        self.assertEqual(items[0]["scope"], "machine")
        self.assertEqual(items[0]["host"], HOST)
        with quiet():
            self.assertIsNone(lore.apply_item(items[0]["uid"], items[0], False))
        self.assertEqual(_entries("machine", HOST), [WIFI])
        self.assertEqual(_entries("user"), [])

    def test_the_prompt_tells_the_model_when_to_choose_machine(self):
        """The reviewer kept choosing "user" because it was the only place
        such a fact could go. A scope the prompt never mentions is a scope the
        deriver will never use."""
        prompt = lore.review_prompt_template()
        self.assertIn("machine", prompt)
        self.assertIn("MACHINE SCOPE", prompt)
        self.assertIn('"scope":"user (true of the person, everywhere) |project|machine',
                      prompt.replace("\n", ""))

    def test_this_hosts_machine_memory_suppresses_a_duplicate_proposal(self):
        """Otherwise a quirk already filed under the box is re-proposed into
        user memory every session -- which is how it got there."""
        lore.memory_add("machine", HOST, WIFI)
        review = {"memory": [{"scope": "user", "action": "add", "text": WIFI}]}
        with quiet(), as_host(HOST):
            self.assertEqual(lore.stage_proposals(review, "-a-repo", "sess-2"), 0)


class TestTheRestOfTheSurface(unittest.TestCase):
    """The places a new scope is silently dropped rather than mishandled."""

    def setUp(self):
        _wipe()

    def test_teardown_hands_machine_memory_back_too(self):
        """Teardown's contract is "leave nothing load-bearing behind", and
        machine memory is the one store NOT reproducible from an op log (it
        does not sync), so a scope teardown skipped would be deleted with the
        store. Every host, not just this one -- they are all facts this store
        holds and nobody else does."""
        lore.memory_add("machine", HOST, WIFI)
        lore.memory_add("machine", "gpu-box", TMPFS)
        with quiet() as out, as_host(HOST):
            rc = lore.cmd_teardown(Namespace(dry_run=True, cwd=TMP))
        self.assertEqual(rc, 0, out.getvalue())
        plan = out.getvalue()
        self.assertIn("lore-export-machine-laptop.md", plan)
        self.assertIn("lore-export-machine-gpu-box.md", plan,
                      "another host's memory is not reproducible anywhere else")

    def test_each_hosts_export_gets_its_own_filename(self):
        """One `lore-export-machine.md` for a whole fleet would have each host
        overwrite the last."""
        a = lore.render_export("machine", HOST, [WIFI])
        b = lore.render_export("machine", "gpu-box", [TMPFS])
        self.assertIn(f"name: lore-export-machine-{HOST}", a)
        self.assertIn("name: lore-export-machine-gpu-box", b)
        self.assertIn(WIFI, a)
        self.assertNotIn(WIFI, b)

    def test_replace_and_remove_work_in_machine_scope(self):
        lore.memory_add("machine", HOST, WIFI)
        self.assertIsNone(lore.memory_replace("machine", HOST, "550 series",
                                              "wifi driver: pin the 560 series"))
        self.assertEqual(_entries("machine", HOST), ["wifi driver: pin the 560 series"])
        self.assertIsNone(lore.memory_remove("machine", HOST, "560 series"))
        self.assertEqual(_entries("machine", HOST), [])
        self.assertEqual([o["payload"] for o in _ops(lore, "memory")], [],
                         "no machine mutation may emit a memory op, not just add")

    def test_the_pile_blocks_and_labels_machine_rows_by_host(self):
        """Two boxes' quirks are not near-duplicates of each other just
        because they are both hardware."""
        a = {"kind": "memory", "scope": "machine", "host": HOST, "text": WIFI}
        b = {"kind": "memory", "scope": "machine", "host": OTHER, "text": WIFI}
        self.assertNotEqual(lore.cluster_key(a), lore.cluster_key(b))
        self.assertEqual(lore.cluster_label(a), f"machine/{HOST}")

    def test_ask_offers_this_hosts_facts_as_evidence_and_not_another_hosts(self):
        lore.memory_add("machine", HOST, WIFI)
        lore.memory_add("machine", OTHER, TMPFS)
        with quiet() as out, as_host(HOST):
            lore.cmd_ask(Namespace(question="wifi driver", cwd=TMP))
        shown = out.getvalue()
        self.assertIn(WIFI, shown)
        self.assertNotIn(TMPFS, shown)


class TestRegistration(unittest.TestCase):
    """A scope nobody is told about is a scope nobody uses -- the issue's own
    diagnosis of why the reviewer kept choosing "user"."""

    def test_the_remember_command_teaches_the_three_way_choice(self):
        doc = (REPO_ROOT / "commands" / "remember.md").read_text(encoding="utf-8")
        self.assertIn("machine", doc)
        self.assertIn("--host", doc)

    def test_help_card_names_the_scope_and_its_knobs(self):
        card = (REPO_ROOT / "commands" / "help.md").read_text(encoding="utf-8")
        self.assertIn("`machine`", card)
        self.assertIn("LORE_MACHINE_CAP", card)

    def test_readme_and_manual_carry_it(self):
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("LORE_MACHINE_CAP", readme)
        manual = (REPO_ROOT / "docs" / "manual.md").read_text(encoding="utf-8")
        self.assertIn("Machine memory", manual)
        self.assertIn("LORE_MACHINE_HOST", manual)
        self.assertIn("--to-machine", manual)
        self.assertIn("does not sync", manual)

    def test_the_caps_are_env_overridable_like_every_other_cap(self):
        self.assertEqual(lore.MACHINE_CAP, 400,
                         "LORE_MACHINE_CAP set at the top of this file must bind")


if __name__ == "__main__":
    unittest.main(verbosity=2)
