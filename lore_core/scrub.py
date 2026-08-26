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
    ("api-key", re.compile(r"sk-[A-Za-z0-9-]{16,}")),
    ("aws", re.compile(r"AKIA[A-Z0-9]{16}")),
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


KV_SECRET = re.compile(
    r"\b(\w*(?:password|passwd|secret|token|api_key|apikey))(\s*[=:]\s*)(\S{8,})",
    re.IGNORECASE,
)

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

HEX_RUN = re.compile(r"\b[a-fA-F0-9]{40,}\b")
BASE64_RUN = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/=])")


def _kv_sub(m: re.Match) -> str:
    key, sep, value = m.group(1), m.group(2), m.group(3)
    if any(pat.match(value) for pat in REFERENCE_SHAPES):
        return m.group(0)
    return f"{key}{sep}[REDACTED:value]"


def _base64_sub(m: re.Match) -> str:
    run = m.group(0)
    # A long absolute path is a 40+ run over the same alphabet ("/" is base64).
    # Digests are full of them via Bash/Read tool lines; redacting paths would
    # gut the index's main value. Slashes with neither "+" nor "=" anywhere in
    # the run is path shape, not credential shape — keep it.
    if "/" in run and "+" not in run and "=" not in run:
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
