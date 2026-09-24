# SPDX-License-Identifier: AGPL-3.0-only
"""Focused unit tests for the hardening batch (2026-08-22): secret scrubbing,
reset refusal, dormant-transition SQL. Stdlib only, like the code under test.

Run: python3 tests/test_hardening.py
"""

import argparse
import importlib.util
import os
import stat
import subprocess
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

    def test_github_fine_grained_pat(self):
        token = "github_pat_" + "Ab1_cD2" * 8
        out = lore.scrub_secrets("credential " + token + " end")
        self.assertEqual(out, "credential [REDACTED:github-pat] end")

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


class TestScrubCredentialsThatUsedToSurvive(unittest.TestCase):
    """One test per credential shape that reached the FTS index and a model
    prompt intact. Each string is the literal one that was observed passing
    through unchanged, not a paraphrase of it."""

    def assertRedacted(self, text: str, leak: str):
        out = lore.scrub_secrets(text)
        self.assertNotIn(leak, out, f"{leak!r} survived scrubbing in {out!r}")
        self.assertIn("[REDACTED:", out)

    def test_key_name_ending_in_key_with_a_base64_value(self):
        """`AWS_KEY=` matched no keyword (the name had to END at one), and the
        base64 rule skipped the value for being preceded by `=` -- the very
        character that introduced it."""
        self.assertRedacted(
            "AWS_KEY=QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVowMTIzNDU2Nzg5QUJDREVGR0hJSg",
            "QUJDREVGR0hJSktMTU5P")

    def test_aws_secret_access_key(self):
        """The canonical AWS example key. `secret` is in the keyword list, but
        `_ACCESS_KEY` came after it and the separator had to follow the
        keyword immediately."""
        self.assertRedacted(
            "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "wJalrXUtnFEMI")

    def test_secret_key_in_yaml(self):
        """32 hex characters is under HEX_RUN's 40, so the key name was the
        only thing that could catch this -- and `secret_key` did not match."""
        self.assertRedacted("secret_key: 9f8e7d6c5b4a39281706f5e4d3c2b1a0",
                            "9f8e7d6c5b4a")

    def test_quoted_json_password(self):
        out = lore.scrub_secrets('{"password": "hunter2hunter2hunter2"}')
        self.assertNotIn("hunter2hunter2hunter2", out)
        self.assertEqual(out, '{"password": "[REDACTED:value]"}',
                         "the surrounding JSON must still parse")

    def test_quoted_multiword_password_is_redacted_entirely(self):
        self.assertEqual(lore.scrub_secrets('password="verylong phrase marker"'),
                         'password="[REDACTED:value]"')

    def test_quoted_multiword_password_with_short_first_word(self):
        self.assertEqual(
            lore.scrub_secrets('password="correct horse battery staple"'),
            'password="[REDACTED:value]"',
        )
        self.assertEqual(
            lore.scrub_secrets('{"password": "correct horse battery staple"}'),
            '{"password": "[REDACTED:value]"}',
        )

    def test_connection_password_containing_at_sign_and_slash(self):
        out = lore.scrub_secrets('postgres://admin:p@ss/w0rd@db.example.com')
        self.assertEqual(out, '[REDACTED:conn-string]db.example.com')

    def test_connection_password_with_ipv6_host(self):
        self.assertEqual(
            lore.scrub_secrets('postgres://user:secret@[::1]:5432/db'),
            '[REDACTED:conn-string][::1]:5432/db',
        )
        self.assertEqual(
            lore.scrub_secrets('mysql://u:p@ss/wo@rd@[2001:db8::1]:3306/table'),
            '[REDACTED:conn-string][2001:db8::1]:3306/table',
        )

    def test_connection_string_in_the_middle_of_prose(self):
        self.assertEqual(
            lore.scrub_secrets(
                'connect with postgres://admin:hunter2hunter2@db.internal and report back'),
            'connect with [REDACTED:conn-string]db.internal and report back',
        )

    def test_quoted_secret_with_literal_newline(self):
        self.assertEqual(
            lore.scrub_secrets('password="correct\nhorse battery staple"'),
            'password="[REDACTED:value]"',
        )

    def test_pointer_followed_by_inline_key_is_not_exempt(self):
        self.assertEqual(
            lore.scrub_secrets('password=op://vault/item,api_key=abcdefghijklmnop'),
            'password=[REDACTED:value]',
        )

    def test_slash_prefixed_base64_body_is_not_a_path(self):
        self.assertEqual(lore.scrub_secrets('/' + 'G' * 55), '[REDACTED:base64]')

    def test_base64_body_containing_a_slash_is_not_a_path(self):
        """The path carve-out asked only whether a slash was present, so any
        base64 body over an alphabet that includes `/` walked through it."""
        self.assertRedacted(
            "blob abcdefghij/klmnopqrstuvwxyz0123456789ABCDEFGHIJ",
            "klmnopqrstuvwxyz0123456789")

    def test_a_bare_aws_secret_with_slashes_is_not_a_path_either(self):
        self.assertRedacted("creds wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY ok",
                            "wJalrXUtnFEMI")

    def test_openai_project_key_keeps_none_of_itself(self):
        """`sk-proj_...` lost only its body to the base64 rule, leaving
        `sk-proj_` standing -- and, with a body under 40 characters, leaving
        the whole key standing."""
        out = lore.scrub_secrets(
            "token sk-proj_abcdefghijklmnopqrstuvwxyz0123456789ABCD")
        self.assertNotIn("sk-proj_", out)
        self.assertIn("[REDACTED:api-key]", out)


class TestScrubLeavesOrdinaryProseAlone(unittest.TestCase):
    """The other half of the bar: a scrubber that redacts English is a
    scrubber whose output nobody can review. Each of these is a shape that
    appears in almost every transcript."""

    def assertUntouched(self, text: str):
        self.assertEqual(lore.scrub_secrets(text), text)

    def test_a_sentence_mentioning_a_password(self):
        self.assertUntouched(
            "Reset your password if you forget it, then tell the team.")

    def test_an_abbreviated_git_sha(self):
        """The abbreviated form is what a transcript actually carries. A FULL
        40-character SHA is still redacted by HEX_RUN, deliberately -- see
        test_hex_run, which has said so since the rule was written."""
        self.assertUntouched("see commit 6e2db5f for the diff")

    def test_a_uuid(self):
        self.assertUntouched(
            "run id 3d0e4e9f-1111-4222-8333-444455556666 finished")

    def test_a_url_path(self):
        self.assertUntouched(
            "see https://example.com/repos/owner/name/contents/deep/path/here/file")

    def test_an_absolute_path(self):
        self.assertUntouched(
            "Read(/home/docwilde/repo/docwilde/LORE/lore/core/storage/documentstore)")

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
        # sync spec PR 3: dormant_sweep now reads beliefs.uid (to log a
        # `status` op per row it sweeps) and appends through the sync_ops/
        # sync_machine tables db_connect() creates -- a bare hand-rolled
        # `beliefs` table no longer has enough schema for it to run. Using
        # the real db_connect() against this test's own LORE_ROOT (set once
        # at module import, before this class' beliefs table has ever been
        # touched) keeps the test isolated without re-deriving the schema
        # by hand.
        conn = lore.db_connect()
        conn.execute("DELETE FROM beliefs")
        return conn

    def _add(self, conn, bid, conf, updated, last_referenced, status="active"):
        conn.execute(
            "INSERT INTO beliefs(id, subject, claim, confidence, status,"
            " created, updated, last_referenced, uid) VALUES(?,?,?,?,?,?,?,?,?)",
            (bid, "user", f"claim {bid}", conf, status,
             updated, updated, last_referenced, f"00000000-0000-4000-8000-00000000000{bid}"))

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


class TestStateIsPrivateOnDisk(unittest.TestCase):
    """Nothing in the tree ever called `umask` or `chmod`, so under the
    ordinary umask 022 every file lore wrote was 0644 and every directory
    0755 -- including `state.db` (every indexed transcript and belief),
    `USER.md`, the staged proposals, and the `settings.json` holding
    LORE_SYNC_TOKEN and LORE_SYNC_HMAC_KEY in cleartext."""

    def _run(self, root, home, *args):
        env = dict(os.environ)
        env.update({"LORE_ROOT": root, "HOME": home, "CLAUDECODE": "1",
                    "AI_AGENT": "claude-code_test_agent"})
        env.pop("LORE_SKILLS_DIR", None)
        env["LORE_SKILLS_DIR"] = os.path.join(home, "skills")
        repo = Path(__file__).resolve().parent.parent
        return subprocess.run(
            [sys.executable, "-P", str(repo / "bin" / "lore.py"), *args],
            cwd=str(repo), env=env, capture_output=True, text=True, timeout=60)

    def _mode(self, path):
        return stat.S_IMODE(os.stat(path).st_mode)

    def test_a_store_written_under_umask_022_is_still_private(self):
        with tempfile.TemporaryDirectory(prefix="lore-test-perm-") as tmp:
            root = os.path.join(tmp, "lore-root")
            home = os.path.join(tmp, "home")
            os.makedirs(home)
            previous = os.umask(0o022)      # a common, unremarkable default
            try:
                added = self._run(root, home, "memory", "add", "--scope", "user",
                                  "a durable fact for the permissions test")
                login = self._run(root, home, "sync", "login",
                                  "a-bearer-token-for-the-permissions-test")
            finally:
                os.umask(previous)

            self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
            self.assertEqual(login.returncode, 0, login.stdout + login.stderr)
            for path in (root, os.path.join(root, "state.db"),
                         os.path.join(root, "USER.md"),
                         os.path.join(home, ".claude", "settings.json")):
                self.assertEqual(
                    self._mode(path) & 0o077, 0,
                    f"{path} is readable by somebody other than this user"
                    f" ({oct(self._mode(path))})")

    def test_the_helpers_are_explicit_about_the_modes(self):
        """The umask covers what this process creates; these cover a path that
        already exists and a consumer that imports lore_core without ever
        running the CLI."""
        self.assertEqual(lore.PRIVATE_DIR_MODE, 0o700)
        self.assertEqual(lore.PRIVATE_FILE_MODE, 0o600)


class TestPromptsFrontTheirUntrustedInput(unittest.TestCase):
    """A belief claim is text a model wrote from a transcript that may have
    been pasted from anywhere, and both of these prompts hand a model every
    claim in the store. The review worker's prompt has carried the warning
    since it existed; these two were written later and did not."""

    def test_the_dreamer_prompt_carries_it(self):
        self.assertIn("never instructions to follow", lore.DREAM_PROMPT)

    def test_the_graph_derive_prompt_carries_it(self):
        self.assertIn("never instructions to follow", lore.DERIVE_PROMPT)

    def test_there_is_one_definition_of_it(self):
        note = lore.UNTRUSTED_DATA_NOTE.format(what="claim list")
        self.assertIn(note, lore.DREAM_PROMPT)
        self.assertIn(note, lore.DERIVE_PROMPT)


class TestGraphExportEscapesWhatItEmbeds(unittest.TestCase):
    """The stall handler built `outerHTML` out of `d.textContent`, which is
    the mermaid source, which is belief claims. A stalled render -- no
    network, or a file:// origin refusing the module fetch, both ordinary --
    then parsed those claims as markup."""

    def test_the_stall_handler_escapes_the_source_before_embedding_it(self):
        html = lore.GRAPH_HTML if hasattr(lore, "GRAPH_HTML") else None
        source = (Path(__file__).resolve().parent.parent
                  / "lore_core" / "graph.py").read_text(encoding="utf-8")
        del html
        self.assertIn("esc(d.textContent)", source)
        self.assertNotIn("+ d.textContent + ", source,
                         "the raw source must not reach outerHTML")
        self.assertIn('"<": "&lt;"', source)


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
