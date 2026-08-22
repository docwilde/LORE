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
HEX_RUN = re.compile(r"\b[a-fA-F0-9]{40,}\b")
BASE64_RUN = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/=])")


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
    text = KV_SECRET.sub(r"\1\2[REDACTED:value]", text)
    text = HEX_RUN.sub("[REDACTED:hex]", text)
    return BASE64_RUN.sub(_base64_sub, text)
