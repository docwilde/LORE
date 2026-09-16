# SPDX-License-Identifier: AGPL-3.0-only
"""Golden tests for docs/sync-protocol.md.

The sync wire contract (docs/sync-protocol.md) pins a canonical JSON
encoding and an HMAC-SHA256 construction that both this repo's future
apply engine and the separate `lore-hub` server must reproduce byte for
byte -- the whole point of a wire contract is that two independent
implementations, reading only the document, land on the same bytes.

This file is that independent implementation, kept deliberately apart
from any `lore_core` module: the apply engine (a later track) owns the
real canonicaliser, and it must be able to adopt these vectors unchanged
rather than the vectors depending on its code existing. So the
canonicaliser and the HMAC helper below are reference code written
straight from the document, not imports from `lore_core`.

Fixtures live in tests/fixtures/sync_protocol/, one JSON file per op. Each
carries the op envelope, the canonical bytes the document says that
envelope signs (as hex, to remove any doubt about the exact byte
sequence), the HMAC-SHA256 over those bytes under the fixed test vector
key in docs/sync-protocol.md Appendix B, and whether the envelope's own
`mac` field is expected to verify against that recomputation.

Run: python3 tests/test_sync_protocol.py
"""

import hashlib
import hmac
import json
import re
import unittest
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "sync_protocol"

# docs/sync-protocol.md Appendix B -- fixed, fake, public. Never use this
# key for anything but reproducing the vectors in this directory.
TEST_KEY = "lore-sync-protocol-test-key-DO-NOT-USE-IN-PRODUCTION"

# The 9 classes docs/sync-protocol.md S3 lists; every fixture's op["class"]
# must be one of these, and every one of these must appear at least once.
KNOWN_CLASSES = {
    "memory",
    "filemap",
    "belief",
    "pending",
    "skill",
    "session",
    "transcript",
    "tabset",
    "worktree",
}


def canonical_bytes(op: dict) -> bytes:
    """docs/sync-protocol.md S2 + S4: the canonical JSON encoding, applied
    to the fixed 8-element signed tuple built from an op envelope.

    Deliberately NOT reused from a shared module -- see the module
    docstring. json.dumps(..., sort_keys=True, separators=(",", ":"),
    ensure_ascii=False) is the document's own reference implementation
    (S2, "Reference implementation"): sorted object keys at every nesting
    level, no insignificant whitespace, literal UTF-8 for every
    non-ASCII character rather than \\uXXXX escapes, Python's
    shortest-round-trip float formatting.
    """
    signed = [
        op["op_id"],
        op["machine_id"],
        op["machine_seq"],
        op["lamport"],
        op["class"],
        op["op"],
        op["project_key"],
        op["payload"],
    ]
    return json.dumps(
        signed, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def compute_mac(op: dict, key: str = TEST_KEY) -> str:
    """docs/sync-protocol.md S4: HMAC-SHA256 over canonical_bytes(op),
    keyed by the UTF-8 bytes of the shared secret, lowercase hex."""
    return hmac.new(
        key.encode("utf-8"), canonical_bytes(op), hashlib.sha256
    ).hexdigest()


def verify_mac(op: dict, key: str = TEST_KEY) -> bool:
    """docs/sync-protocol.md S5.1: constant-time comparison of the
    recomputed mac against the mac field carried on the envelope. A
    missing (None) mac never verifies -- S5.2/S5.3."""
    received = op.get("mac")
    if not received:
        return False
    return hmac.compare_digest(compute_mac(op, key), received)


def load_fixtures() -> dict[str, dict]:
    fixtures = {}
    for path in sorted(FIXTURES_DIR.glob("*.json")):
        fixtures[path.stem] = json.loads(path.read_text(encoding="utf-8"))
    return fixtures


FIXTURES = load_fixtures()


class TestFixturesArePresent(unittest.TestCase):
    def test_the_fixture_directory_is_not_empty(self):
        self.assertTrue(FIXTURES, f"no fixtures found under {FIXTURES_DIR}")

    def test_every_known_class_has_at_least_one_fixture(self):
        seen = {fx["op"]["class"] for fx in FIXTURES.values()}
        missing = KNOWN_CLASSES - seen
        self.assertFalse(missing, f"no fixture for class(es): {sorted(missing)}")

    def test_at_least_one_fixture_has_a_null_project_key(self):
        self.assertTrue(
            any(fx["op"]["project_key"] is None for fx in FIXTURES.values()),
            "no fixture exercises project_key: null",
        )

    def test_at_least_one_fixture_carries_non_ascii_text(self):
        def has_non_ascii(value: object) -> bool:
            if isinstance(value, str):
                return any(ord(ch) > 127 for ch in value)
            if isinstance(value, dict):
                return any(has_non_ascii(v) for v in value.values())
            if isinstance(value, list):
                return any(has_non_ascii(v) for v in value)
            return False

        self.assertTrue(
            any(has_non_ascii(fx["op"]["payload"]) for fx in FIXTURES.values()),
            "no fixture payload carries a non-ASCII character",
        )

    def test_at_least_one_fixture_is_deliberately_tampered(self):
        self.assertTrue(
            any(fx["mac_should_verify"] is False for fx in FIXTURES.values()),
            "no fixture exercises a tampered / failing mac",
        )


class TestCanonicalBytesMatchTheDocument(unittest.TestCase):
    """Every fixture's canonical_bytes_hex is what docs/sync-protocol.md
    S2's algorithm produces from the op fields, byte for byte."""

    def test_every_fixture(self):
        for name, fx in FIXTURES.items():
            with self.subTest(fixture=name):
                expected = bytes.fromhex(fx["canonical_bytes_hex"])
                actual = canonical_bytes(fx["op"])
                self.assertEqual(
                    actual,
                    expected,
                    f"{name}: canonical bytes diverge from the fixture",
                )

    def test_canonical_bytes_contain_no_insignificant_whitespace(self):
        """S2.2: no whitespace between tokens. Checked structurally, not by
        a raw substring search -- a payload string is free to contain its
        own ", " or ": " (several fixtures' text fields do), which a naive
        substring check would misreport as a serializer-inserted space."""
        for name, fx in FIXTURES.items():
            with self.subTest(fixture=name):
                text = canonical_bytes(fx["op"]).decode("utf-8")
                self.assertNotIn("\n", text, f"{name}: newline in canonical bytes")
                self.assertNotIn("\t", text, f"{name}: tab in canonical bytes")
                # Strip string literals (JSON strings, respecting \" and \\
                # escapes) so only structural characters remain, then check
                # those for the separators a non-compact encoder inserts.
                structure = re.sub(r'"(?:[^"\\]|\\.)*"', '""', text)
                self.assertNotIn(", ", structure, f"{name}: space after a structural comma")
                self.assertNotIn(": ", structure, f"{name}: space after a structural colon")
                self.assertNotIn("  ", structure, f"{name}: repeated space outside strings")

    def test_canonical_bytes_is_a_flat_8_element_array(self):
        for name, fx in FIXTURES.items():
            with self.subTest(fixture=name):
                decoded = json.loads(bytes.fromhex(fx["canonical_bytes_hex"]))
                self.assertIsInstance(decoded, list)
                self.assertEqual(len(decoded), 8)

    def test_non_ascii_characters_are_emitted_literally_not_escaped(self):
        """S2.5: ensure_ascii=False -- a non-ASCII character appears as its
        literal UTF-8 bytes, never as a \\uXXXX escape."""
        fx = FIXTURES["memory_add_unicode"]
        text = canonical_bytes(fx["op"]).decode("utf-8")
        self.assertIn("über", text)
        self.assertIn("😀", text)
        self.assertIn("北京", text)
        self.assertNotIn("\\u00fc", text)  # the escaped form of "ü"


class TestMacMatchesTheDocument(unittest.TestCase):
    """Every fixture's expected_mac_hex is HMAC-SHA256(TEST_KEY,
    canonical_bytes) per docs/sync-protocol.md S4."""

    def test_every_fixture(self):
        for name, fx in FIXTURES.items():
            with self.subTest(fixture=name):
                self.assertEqual(
                    compute_mac(fx["op"]),
                    fx["expected_mac_hex"],
                    f"{name}: recomputed mac diverges from the fixture",
                )

    def test_mac_is_64_lowercase_hex_characters(self):
        for name, fx in FIXTURES.items():
            with self.subTest(fixture=name):
                mac = fx["expected_mac_hex"]
                self.assertEqual(len(mac), 64, name)
                self.assertEqual(mac, mac.lower(), name)
                int(mac, 16)  # raises ValueError if it is not hex


class TestVerificationOutcomeMatchesTheFixture(unittest.TestCase):
    """docs/sync-protocol.md S5.1: a receiver recomputes the mac from the
    op as received and compares to the envelope's own mac field. Every
    fixture declares the outcome that comparison must produce."""

    def test_every_fixture(self):
        for name, fx in FIXTURES.items():
            with self.subTest(fixture=name):
                self.assertEqual(
                    verify_mac(fx["op"]),
                    fx["mac_should_verify"],
                    f"{name}: verification outcome diverges from the fixture",
                )

    def test_the_tampered_fixture_fails_because_the_payload_moved(self):
        """Not just "some fixture fails" -- pin *why* this one fails, so a
        future edit that breaks the tamper for an unrelated reason (e.g. a
        typo in op_id) is still caught as wrong."""
        fx = FIXTURES["belief_reinforce_tampered"]
        self.assertFalse(fx["mac_should_verify"])
        self.assertFalse(verify_mac(fx["op"]))
        # The envelope's own mac is stale: it was computed over a payload
        # this fixture no longer carries (S5.2's "absent or wrong").
        self.assertNotEqual(fx["op"]["mac"], fx["expected_mac_hex"])
        self.assertNotEqual(compute_mac(fx["op"]), fx["op"]["mac"])

    def test_a_wrong_key_fails_every_fixture_even_the_correctly_signed_ones(self):
        wrong_key = "not-the-real-test-key"
        for name, fx in FIXTURES.items():
            if not fx["mac_should_verify"]:
                continue
            with self.subTest(fixture=name):
                self.assertFalse(verify_mac(fx["op"], key=wrong_key), name)

    def test_a_missing_mac_never_verifies(self):
        """S5.2/S5.3: null and "wrong" are the same failure -- neither is
        ever silently trusted."""
        fx = FIXTURES["memory_add"]
        op = dict(fx["op"])
        op["mac"] = None
        self.assertFalse(verify_mac(op))


if __name__ == "__main__":
    unittest.main(verbosity=2)
