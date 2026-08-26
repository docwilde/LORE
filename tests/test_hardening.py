# SPDX-License-Identifier: AGPL-3.0-only
"""Focused unit tests for the hardening batch (2026-08-22): secret scrubbing,
reset refusal, dormant-transition SQL. Stdlib only, like the code under test.

Run: python3 tests/test_hardening.py
"""

import argparse
import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

# LORE_ROOT must point away from the real store BEFORE the module executes:
# lore.py reads it at import time into module constants.
os.environ["LORE_ROOT"] = tempfile.mkdtemp(prefix="lore-test-")

_spec = importlib.util.spec_from_file_location(
    "lore", Path(__file__).resolve().parent.parent / "bin" / "lore.py")
lore = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lore)


class TestScrubSecrets(unittest.TestCase):
    def test_openai_style_key(self):
        out = lore.scrub_secrets("key is sk-" + "A1b2" * 6 + " ok")
        self.assertIn("[REDACTED:api-key]", out)
        self.assertNotIn("sk-A1b2", out)

    def test_openrouter_key_gets_its_own_kind(self):
        # sk-or-v1 must win over the generic sk- prefix (pattern order).
        out = lore.scrub_secrets("sk-or-v1-" + "ab12" * 12)
        self.assertIn("[REDACTED:openrouter]", out)
        self.assertNotIn("[REDACTED:api-key]", out)

    def test_aws_access_key(self):
        out = lore.scrub_secrets("export AWS=AKIA" + "A7" * 8)
        self.assertIn("[REDACTED:aws]", out)

    def test_github_tokens(self):
        for prefix in ("ghp_", "gho_"):
            out = lore.scrub_secrets(prefix + "Ab1" * 12)
            self.assertIn("[REDACTED:github]", out, prefix)

    def test_cloudflare_token(self):
        out = lore.scrub_secrets("cfat_" + "Zz9" * 8)
        self.assertIn("[REDACTED:cloudflare]", out)

    def test_bearer_header(self):
        out = lore.scrub_secrets("Authorization: Bearer abcDEF123456789012345678._~")
        self.assertIn("[REDACTED:bearer]", out)
        self.assertNotIn("abcDEF", out)

    def test_key_value_keeps_key_redacts_value(self):
        out = lore.scrub_secrets("set GITHUB_TOKEN=hunter2hunter2")
        self.assertIn("GITHUB_TOKEN=[REDACTED:value]", out)
        out = lore.scrub_secrets("password: correcthorsebattery")
        self.assertIn("password: [REDACTED:value]", out)

    def test_short_kv_value_untouched(self):
        # \S{8,}: a 7-char value is below the credential floor.
        text = "token: short12"
        self.assertEqual(lore.scrub_secrets(text), text)

    def test_op_reference_survives(self):
        # The bug this guards: an op:// pointer is safe to keep verbatim --
        # resolving it needs the 1Password vault, which scrub_secrets never
        # has. Redacting it destroys a command nobody can then run.
        out = lore.scrub_secrets(
            "op run --env-file=<(echo 'GITLAB_TOKEN=op://Employee/Telekom "
            "GLab PAT/<field>') -- glab api user")
        self.assertIn("GITLAB_TOKEN=op://Employee/Telekom", out)
        self.assertNotIn("REDACTED", out)

    def test_op_reference_paired_with_real_token(self):
        # Same key, same shape of command -- but the value is material, not
        # a pointer, so it must still redact.
        out = lore.scrub_secrets("GITLAB_TOKEN=glpat-xxxxxxxxxxxxxxxxxxxx")
        self.assertIn("GITLAB_TOKEN=[REDACTED:value]", out)
        self.assertNotIn("glpat-", out)

    def test_vault_reference_survives(self):
        out = lore.scrub_secrets("DB_PASSWORD=vault:secret/data/myapp#password")
        self.assertIn("DB_PASSWORD=vault:secret/data/myapp#password", out)
        self.assertNotIn("REDACTED", out)

    def test_vault_reference_paired_with_real_password(self):
        out = lore.scrub_secrets("DB_PASSWORD=hunter2hunter2")
        self.assertIn("DB_PASSWORD=[REDACTED:value]", out)
        self.assertNotIn("hunter2hunter2", out)

    def test_vault_url_scheme_reference_survives(self):
        out = lore.scrub_secrets("API_TOKEN=vault://secret/foo/bar")
        self.assertIn("API_TOKEN=vault://secret/foo/bar", out)
        self.assertNotIn("REDACTED", out)

    def test_vault_url_scheme_paired_with_real_token(self):
        out = lore.scrub_secrets("API_TOKEN=abcdefgh12345678")
        self.assertIn("API_TOKEN=[REDACTED:value]", out)
        self.assertNotIn("abcdefgh12345678", out)

    def test_keyring_reference_survives(self):
        out = lore.scrub_secrets("SECRET=keyring://myservice/myaccount")
        self.assertIn("SECRET=keyring://myservice/myaccount", out)
        self.assertNotIn("REDACTED", out)

    def test_keyring_reference_paired_with_real_secret(self):
        out = lore.scrub_secrets("SECRET=correcthorsebattery")
        self.assertIn("SECRET=[REDACTED:value]", out)
        self.assertNotIn("correcthorsebattery", out)

    def test_braced_var_expansion_survives(self):
        out = lore.scrub_secrets("TOKEN=${MY_TOKEN}")
        self.assertEqual(out, "TOKEN=${MY_TOKEN}")

    def test_bare_var_expansion_survives(self):
        out = lore.scrub_secrets("TOKEN=$MY_SECRET_TOKEN")
        self.assertEqual(out, "TOKEN=$MY_SECRET_TOKEN")

    def test_var_expansion_paired_with_real_token(self):
        # Same key, value that is NOT a $VAR shape -- must still redact.
        out = lore.scrub_secrets("TOKEN=notarealvarname12345")
        self.assertIn("TOKEN=[REDACTED:value]", out)
        self.assertNotIn("notarealvarname12345", out)

    def test_placeholder_survives(self):
        out = lore.scrub_secrets("API_KEY=<your-key-here>")
        self.assertEqual(out, "API_KEY=<your-key-here>")

    def test_placeholder_paired_with_real_key(self):
        out = lore.scrub_secrets("API_KEY=abcdefgh12345678")
        self.assertIn("API_KEY=[REDACTED:value]", out)
        self.assertNotIn("abcdefgh12345678", out)

    def test_pem_block(self):
        pem = ("-----BEGIN RSA PRIVATE KEY-----\n"
               "MIIEpAIBAAKCAQEA7\nmoremoremore\n"
               "-----END RSA PRIVATE KEY-----")
        out = lore.scrub_secrets(f"here {pem} there")
        self.assertEqual(out, "here [REDACTED:pem] there")

    def test_hex_run(self):
        sha = "deadbeef" * 5  # 40 hex chars — full git SHA, sacrificed by design
        out = lore.scrub_secrets(f"commit {sha} pushed")
        self.assertEqual(out, "commit [REDACTED:hex] pushed")

    def test_base64_run(self):
        blob = "QUJ+" * 12 + "=="
        out = lore.scrub_secrets(f"blob {blob} end")
        self.assertIn("[REDACTED:base64]", out)

    def test_long_path_survives_base64_rule(self):
        text = "Read: /home/user/some/very/long/nested/path/to/the/artifact/file"
        self.assertEqual(lore.scrub_secrets(text), text)

    def test_clean_text_passthrough(self):
        text = ("The resolver caps workers at 15; run lore search foo_bar "
                "and check state.db counts.")
        self.assertEqual(lore.scrub_secrets(text), text)

    def test_build_digest_scrubs(self):
        digest = lore.build_digest([("", "user", "my api_key=sk-" + "x1" * 12)])
        self.assertNotIn("sk-x1", digest)
        self.assertIn("REDACTED", digest)


class TestResetRefusal(unittest.TestCase):
    def test_no_flag_refuses(self):
        args = argparse.Namespace(index=False, beliefs=False, all=False)
        self.assertEqual(lore.cmd_reset(args), 1)


class TestDormantSweep(unittest.TestCase):
    def _conn(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE beliefs("
            "id INTEGER PRIMARY KEY, subject TEXT, claim TEXT, confidence REAL,"
            "status TEXT DEFAULT 'active', superseded_by INTEGER, resolution TEXT,"
            "created TEXT, updated TEXT, last_referenced TEXT)")
        return conn

    def _add(self, conn, bid, conf, updated, last_referenced, status="active"):
        conn.execute(
            "INSERT INTO beliefs(id, subject, claim, confidence, status,"
            " created, updated, last_referenced) VALUES(?,?,?,?,?,?,?,?)",
            (bid, "user", f"claim {bid}", conf, status,
             updated, updated, last_referenced))

    def test_transitions(self):
        conn = self._conn()
        old, fresh = "2026-01-01T00:00:00Z", "2026-08-21T00:00:00Z"
        self._add(conn, 1, 0.7, old, old)         # stale, low conf -> dormant
        self._add(conn, 2, 0.97, old, old)        # stale, near-certain -> stays
        self._add(conn, 3, 0.7, fresh, fresh)     # fresh -> stays
        self._add(conn, 4, 0.7, old, None)        # null ref, old updated -> dormant
        self._add(conn, 5, 0.7, old, fresh)       # recently referenced -> stays
        self._add(conn, 6, 0.5, old, old, "retracted")  # not active -> untouched
        moved = lore.dormant_sweep(conn, days=45)
        self.assertEqual(moved, 2)
        status = dict(conn.execute("SELECT id, status FROM beliefs"))
        self.assertEqual(status[1], "dormant")
        self.assertEqual(status[2], "active")
        self.assertEqual(status[3], "active")
        self.assertEqual(status[4], "dormant")
        self.assertEqual(status[5], "active")
        self.assertEqual(status[6], "retracted")


if __name__ == "__main__":
    unittest.main(verbosity=2)


import importlib.util as _ilu
from pathlib import Path as _P
_spec = _ilu.spec_from_file_location("lore_s", _P(__file__).resolve().parent.parent / "bin" / "lore.py")
_lore_s = _ilu.module_from_spec(_spec); _spec.loader.exec_module(_lore_s)


def test_scrub_new_patterns():
    # token shapes assembled from fragments so repo secret-scanning never sees
    # a literal credential in the test source (it flagged the fixtures once).
    s = _lore_s.scrub_secrets
    jwt = "eyJ" + "hbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiIxMjM0NTY3ODkwfQ" + "." + "abcDEFghiJKLmnoPQRstuvWX012"
    assert "eyJ" not in s("token " + jwt)
    assert "secretpass" not in s("db at postgres://admin:" + "secretpass" + "@localhost:5432/x")
    assert "_live_" not in s("stripe " + "sk" + "_live_" + "abcdefghij0123456789")
    slack = "xox" + "b-" + "1234567890-" + "abcdefghijklmno"
    assert "xox" + "b-" not in s("slack " + slack)
    assert "AIza" not in s("gcp " + "AIza" + "SyABCDEFGHIJKLMNOPQRSTUVWXYZ0123456")


def test_scrub_before_truncate_via_derive_output():
    # deriver output scrub: a JWT in a claim must not persist
    import sqlite3
    jwt = "eyJ" + "hbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiIxMjM0NTY3ODkwfQ" + "." + "abcDEFghiJKLmnoPQRstuvWX012"
    n = _lore_s.derive_conclusions(
        {"conclusions": [{"scope": "project", "claim": "key is " + jwt,
                          "confidence": 0.7, "evidence": "seen"}]},
        "slug-scrub", "sess-scrub")
    assert n == 1
    conn = _lore_s.db_connect()
    claim = conn.execute("SELECT claim FROM beliefs WHERE claim LIKE '%REDACTED%' OR claim LIKE '%eyJ%'").fetchone()
    assert claim is not None and "eyJ" not in claim[0]


def test_interaction_model_wired_into_context():
    import inspect
    assert "interaction_model_lines(" in inspect.getsource(_lore_s.build_context)


def test_refresh_on_change_default_and_optout(monkeypatch):
    from lore_core.context import refresh_on_change
    monkeypatch.delenv("LORE_REFRESH_ON_CHANGE", raising=False)
    assert refresh_on_change() is True
    monkeypatch.setenv("LORE_REFRESH_ON_CHANGE", "0")
    assert refresh_on_change() is False


def test_review_interval_parsing(monkeypatch):
    from lore_core.context import review_interval
    monkeypatch.delenv("LORE_REVIEW_SECS", raising=False)
    assert review_interval() is None
    monkeypatch.setenv("LORE_REVIEW_SECS", "3600")
    assert review_interval() == 3600
    monkeypatch.setenv("LORE_REVIEW_SECS", "junk")
    assert review_interval() is None


def test_refresh_state_roundtrip_and_legacy(tmp_path):
    from lore_core.context import _read_refresh_state, _write_refresh_state
    p = tmp_path / "stamp"
    _write_refresh_state(p, 1234.0, "abc123")
    ts, h = _read_refresh_state(p)
    assert ts == 1234.0 and h == "abc123"
    p.write_text("999")  # pre-0.33.0 format: timestamp only
    ts, h = _read_refresh_state(p)
    assert ts == 999.0 and h is None
