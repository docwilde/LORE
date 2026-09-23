# SPDX-License-Identifier: AGPL-3.0-only
"""Secret scrubbing -- the ingest choke point.

Every place a transcript or a model's own output is about to be written to
persistent state (the FTS index) or re-sent to a model (a deriver/dreamer
prompt built from a digest) MUST pass its text through scrub_secrets() first.
That is the entire contract of this module: nothing downstream can be
trusted to have scrubbed on its own, so store.py, deriver.py and dialectic
callers all route through the single scrub_secrets() defined here rather
than reimplementing any part of it.

Ordering inside SECRET_PATTERNS is load-bearing: PEM before the base64 run (a
key body IS one long base64 run), sk-or-v1 before the generic sk- prefix
(which would eat it under the wrong label), hex before base64 (hex is a
subset of the base64 alphabet). See scrub_secrets() below for the rest.
"""

import re


__all__ = [
    'SECRET_PATTERNS',
    'KV_SECRET',
    'REFERENCE_SHAPES',
    'HEX_RUN',
    'BASE64_RUN',
    'scrub_secrets',
]

SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("pem", re.compile(r"-----BEGIN [^-]+-----.*?-----END [^-]+-----", re.DOTALL)),
    # JWT before the generic base64/hex rules: three base64url segments dotted.
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    # credentials embedded in a connection string: scheme://user:pass@host
    ("conn-string", re.compile(r"([a-z][a-z0-9+.\-]*://[^\s:/@]+:)([^\s/@]{3,})(@)", re.IGNORECASE)),
    ("openrouter", re.compile(r"sk-or-v1-[a-f0-9]+")),
    # stripe/openai-style live/test secret + restricted keys (underscore form)
    ("provider-secret", re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}")),
    # `_` as well as `-`: OpenAI's project keys spell themselves `sk-proj_...`,
    # and a class that stopped at the underscore matched only `sk-proj`, which
    # is too short to trip the {16,} floor. The key then fell through to the
    # base64 rule, which redacted the body and left `sk-proj_` standing --
    # and, under 40 characters of body, left the whole key standing.
    ("api-key", re.compile(r"sk-[A-Za-z0-9_-]{16,}")),
    ("aws", re.compile(r"AKIA[A-Z0-9]{16}")),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}")),
    ("github", re.compile(r"gh[posru]_[A-Za-z0-9]{36,}")),
    ("gcp", re.compile(r"AIza[A-Za-z0-9_-]{35}")),
    ("slack", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("slack-app", re.compile(r"xapp-[0-9]-[A-Za-z0-9-]{20,}")),
    ("npm", re.compile(r"npm_[A-Za-z0-9]{36}")),
    ("pypi", re.compile(r"pypi-AgEIcHlwaS[A-Za-z0-9_-]{16,}")),
    ("cloudflare", re.compile(r"cfat_[A-Za-z0-9]{20,}")),
    ("bearer", re.compile(r"Bearer\s+[A-Za-z0-9._~+/-]{20,}")),
    ("basic-auth", re.compile(r"Basic\s+[A-Za-z0-9+/]{16,}={0,2}")),
]


# `<key-ish name> = <value>` in any of the spellings a transcript actually
# carries. Three things this has to get right, each of which it used to get
# wrong and each of which left a real credential standing:
#
#   * THE NAME MAY CONTINUE PAST THE KEYWORD. `\w*` on the LEFT only meant the
#     keyword had to be the last thing before the separator, so `AWS_SECRET_
#     ACCESS_KEY=`, `secret_key:` and `api_key_prod=` all missed. `\w*` on both
#     sides fixes every one of them.
#   * `_key` IS ITS OWN KEYWORD, so `AWS_KEY=` and `LORE_SYNC_HMAC_KEY=` match.
#     Deliberately `_key` and not `key`: the underscore is what makes it a
#     config variable rather than `monkey`, `hotkey` or `keyboard`.
#   * THE PAIR MAY BE QUOTED. `{"password": "hunter2hunter2hunter2"}` is the
#     shape a pasted JSON body has, and a separator group that allowed no
#     quotes could not reach the value at all. The quotes are captured into
#     the separator and the value's own trailing punctuation is handed back by
#     `_kv_sub`, so the surrounding JSON still parses after redaction.
KV_SECRET = re.compile(
    r"\b(\w*(?:password|passwd|secret|token|credential|api_key|apikey|_key)\w*)"
    r"([\"']?\s*[=:]\s*[\"']?)"
    r"(\S{8,})",
    re.IGNORECASE,
)

# Trailing characters a value picked up from the text around it rather than
# from the secret: the closing quote of a JSON string, a comma, a closing
# brace. Stripped before the reference-shape test and handed back after the
# redaction, so `{"password": "..."}` stays a JSON object.
_VALUE_TRAILERS = "\"'`,;)]}>"

# Value shapes that are a POINTER to a secret, not the secret material —
# resolving one back into material needs the vault/keyring/shell it names,
# which this scrubber never has. Redacting the pointer instead of the
# material is its own failure: it destroys the one part of a command that
# was safe to keep. (Observed live: an `op://` reference in a DOXA
# transcript rendered as `[REDACTED:value]`, leaving a command nobody could
# run.) Each pattern anchors the WHOLE captured value (\A...\Z) — a scheme
# prefix is a distinct, well-specified shape, not just a string to strip, so
# a value that only partly looks like one still redacts as material. Kept
# deliberately short: under-redaction leaks a credential, over-redaction
# only mangles a command, and those costs are not symmetric — a shape
# without a citable, standard "this value is a pointer" convention stays
# OUT rather than being guessed at. aws-vault:, gopass: and pass: were all
# considered and left out on that basis: each is primarily an exec-wrapper
# CLI (`aws-vault exec profile -- cmd`, `pass show path`), not an
# established inline value-reference scheme the way op://, vault:// and
# keyring:// are, so anchoring against them would be inventing a shape, not
# citing one.
REFERENCE_SHAPES: list[re.Pattern] = [
    re.compile(r"\Aop://\S+\Z"),                           # 1Password CLI reference
    re.compile(r"\Avault(?:://|:)\S+\Z", re.IGNORECASE),   # HashiCorp Vault path
    re.compile(r"\Akeyring://\S+\Z", re.IGNORECASE),       # OS/credential-keyring reference
    re.compile(r"\A\$\{[A-Za-z_][A-Za-z0-9_]*\}\Z"),       # ${VAR} shell expansion
    re.compile(r"\A\$[A-Za-z_][A-Za-z0-9_]*\Z"),           # $VAR shell expansion
    re.compile(r"\A<[^<>\s]+>\Z"),                          # <placeholder> in example commands
]

# The longest a `/`-delimited part of a run may be for the whole run to still
# read as a path rather than as material. Measured against what each side
# actually looks like: the parts of a path or a URL are words a human typed
# (`storage`, `contents`, `documentstore`), while base64 material has no word
# boundaries in it at all.
_PATH_SEGMENT_MAX = 16

HEX_RUN = re.compile(r"\b[a-fA-F0-9]{40,}\b")
# The lookbehind no longer excludes `=`. It did, and so `AWS_KEY=<40 chars of
# base64>` -- the single most ordinary way a key appears in a shell line --
# was skipped for being preceded by the very character that introduced it.
# Padding of a preceding run is not a reason to skip: that run consumed its
# own `={0,2}` already.
BASE64_RUN = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/=])")


def _kv_sub(m: re.Match) -> str:
    key, sep, value = m.group(1), m.group(2), m.group(3)
    # The WHOLE value first: `${MY_TOKEN}` and `<your-key-here>` end in a
    # character `_VALUE_TRAILERS` would otherwise eat, and a pointer with its
    # closing brace removed is no longer a recognisable pointer.
    if any(pat.match(value) for pat in REFERENCE_SHAPES):
        return m.group(0)
    trail = ""
    while value and value[-1] in _VALUE_TRAILERS:
        trail, value = value[-1] + trail, value[:-1]
    if not value or any(pat.match(value) for pat in REFERENCE_SHAPES):
        return m.group(0)
    return f"{key}{sep}[REDACTED:value]{trail}"


def _base64_sub(m: re.Match) -> str:
    run = m.group(0)
    # A long absolute path is a 40+ run over the same alphabet ("/" is base64).
    # Digests are full of them via Bash/Read tool lines and via URLs, and
    # redacting paths would gut the index's main value — so a run that STARTS
    # with "/" and carries neither "+" nor "=" is kept.
    #
    # "Contains a slash" alone was not path shape. It let
    # `abcdefghij/klmnopqrstuvwxyz0123456789ABCDEFGHIJ` through untouched, and
    # it let an AWS secret access key through too -- a base64 body whose
    # alphabet happens to include the separator is not a path, and nothing
    # about it said it was. Two things say it: a path begins at a slash, and
    # the parts of a path and of a URL are WORDS. A secret is one long
    # unbroken stretch, so a run whose longest run between slashes is 16
    # characters or more is treated as material even when it has slashes in
    # it. A directory with a very long name is redacted by that; the costs are
    # not symmetric (see scrub_secrets' own docstring) and this is the cheap
    # side.
    if "+" in run or "=" in run:
        return "[REDACTED:base64]"
    if run.startswith("/"):
        return run
    if "/" in run and max(len(part) for part in run.split("/")) < _PATH_SEGMENT_MAX:
        return run
    return "[REDACTED:base64]"


def scrub_secrets(text: str) -> str:
    """Credential-shaped substrings replaced with [REDACTED:<kind>].

    Applied per message at both ingestion points (build_digest, index_sessions)
    rather than once at display: a secret that never lands in state.db or a
    worker prompt cannot leak from either, whatever new consumer is added later.
    False positives are accepted by design — a mangled hex string in a digest
    costs a worse review; a replayed credential costs a rotation.
    """
    for kind, pat in SECRET_PATTERNS:
        text = pat.sub(f"[REDACTED:{kind}]", text)
    text = KV_SECRET.sub(_kv_sub, text)
    text = HEX_RUN.sub("[REDACTED:hex]", text)
    return BASE64_RUN.sub(_base64_sub, text)
