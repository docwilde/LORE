# The sync wire contract

Status: **normative, v1**. This document is the contract between LORE
clients (`lore_core`, any machine) and a sync server — the hub
(`docwilde/lore-hub`) or a Transport B peer (`lore sync serve`). Both
sides' CI test against it. Where this document and
[`docs/plans/sync.md`](plans/sync.md) disagree on wire-level detail, this
document wins; `sync.md` is the design rationale, this is the byte-level
promise. Where this document is silent, `sync.md`'s merge rules (the
per-class verb table) govern application-level behaviour, which is out of
scope here — this document pins the *wire*: encoding, signing, transport,
not what a receiver's local store does with an applied op.

The keywords MUST, MUST NOT, SHOULD, SHOULD NOT, and MAY are used
throughout and are to be interpreted as in [RFC 2119](https://www.rfc-editor.org/rfc/rfc2119).

Golden test vectors for every rule below live in
[`tests/fixtures/sync_protocol/`](../tests/fixtures/sync_protocol/) and are
checked byte-for-byte by
[`tests/test_sync_protocol.py`](../tests/test_sync_protocol.py):

```
python3 tests/test_sync_protocol.py
```

A server implementation (in any language) MUST be able to reproduce every
`canonical_bytes_hex` and `expected_mac_hex` value in those fixtures from
the `op` object alone, using nothing but this document.

## 1. Scope

This contract fixes, identically for both transports:

- the canonical JSON encoding used only for computing and verifying `mac`
- the op envelope: its fields, their types, and which of them are signed
- the HMAC-SHA256 construction, and what a receiver does when it fails
- the hub's HTTP endpoints: request/response shape, status codes, paging
- what a Transport B peer MUST reproduce of the hub's contract, and what
  it MAY omit
- idempotency rules for both push and apply

It does not fix: the per-class merge semantics (`sync.md`, "Verbs, per
class, and the merge rules"), the local `sync_ops` schema, or anything
about `lore sync`'s CLI surface. Appendix A reproduces the per-class
payload shapes for convenience only; `sync.md` is authoritative for their
semantics.

**Transport neutrality.** Nothing in §2–§5 (canonical encoding, the op
envelope, the HMAC, and MAC-verification behaviour) may assume a hub is
involved. An op signed for a Transport B peer pull and an op signed for a
hub push are byte-identical. Only §6–§7 (HTTP endpoints) distinguish the
two transports, and §7 exists to say how little is different.

## 2. Canonical JSON encoding

The canonical encoding is used for exactly one purpose: producing the
byte string that `mac` in §4 signs. It is never used to store or transmit
an op — the wire representation of an op (§3, and every HTTP body in §6)
is ordinary JSON and MAY be pretty-printed, MAY reorder object keys, MAY
use `\uXXXX` escapes, by any library on either side. Only the signing step
uses this algorithm.

Given a JSON value (built from the op's fields as described in §4),
produce bytes as follows:

1. **Encoding.** The output is UTF-8 bytes. No byte order mark.
2. **Whitespace.** None. No space, tab, or newline appears anywhere
   between tokens. Item separator is `,`, key separator is `:` — i.e. the
   two-character sequences `", "` and `": "` that a default JSON
   serializer often inserts MUST NOT appear; only the bare `,` and `:`
   characters do.
3. **Object member order.** At every level of nesting, object members are
   sorted in ascending order of their key, compared code point by code
   point. (This is equivalent to ascending order of the key's UTF-8 byte
   sequence, because UTF-8 preserves code point ordering under
   byte-lexicographic comparison — the same property [RFC 8785 §3.2.3
   (JCS)](https://www.rfc-editor.org/rfc/rfc8785) relies on.) This applies
   recursively to every nested object, including inside `payload`.
4. **Array order.** Preserved exactly as constructed. Arrays are never
   reordered. The top-level value signed (§4) is itself an array, and its
   8 positions are fixed by this document, never sorted.
5. **Strings.** Enclosed in `"`. `"` (U+0022) and `\` (U+005C) MUST be
   escaped as `\"` and `\\`. Control characters U+0000–U+001F MUST be
   escaped: the short forms `\b` `\f` `\n` `\r` `\t` for U+0008, U+000C,
   U+000A, U+000D, U+0009 respectively, and `\u00xx` (lowercase hex) for
   every other character in that range. `/` (U+002F) MUST NOT be escaped.
   Every other Unicode scalar value — including U+007F and all non-ASCII
   characters — MUST be emitted as its literal UTF-8 byte sequence, never
   as a `\uXXXX` escape. (This is `ensure_ascii=False`, not Python's
   `json` default.)
6. **Integers.** `machine_seq` and `lamport` (the only bare integers in
   the signed tuple; payloads MAY carry their own) are encoded with no
   leading zeros (except the literal `0`), no leading `+`, no fractional
   part, no exponent.
7. **Floating-point numbers.** (E.g. a belief's `confidence`.) Encoded as
   the shortest decimal representation that round-trips to the same IEEE
   754 binary64 value, with a decimal point and at least one digit on
   each side (`0.87`, `1.0`, not `.87` or `1`), no exponent unless the
   shortest round-trip form requires one. This is exactly what CPython's
   `repr(float)` / `json.dumps` have produced since 3.1. A payload float
   whose canonical digit count differs from this form will not verify —
   round through `float` before signing, never hand-format.
8. **`null`.** The four-character literal `null` — used for `project_key`
   when an op is user-scoped, and for any payload field the per-class
   shape (Appendix A) marks nullable.
9. **Booleans.** `true` / `false`.
10. **Not permitted anywhere in a signed value:** `NaN`, `Infinity`,
    `-Infinity`, trailing commas, comments. A sender MUST reject (locally,
    before ever computing a `mac`) an op whose payload would require one
    of these.

**Reference implementation.** Because every known implementer of this
contract (LORE, lore-hub) is Python, the algorithm above is exactly:

```python
import json

def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
```

applied to the 8-element array of §4. This is normative for this
codebase: on any disagreement between the prose above and this snippet's
output on a fixture in `tests/fixtures/sync_protocol/`, the fixture (and
therefore this snippet) wins, and the prose MUST be corrected to match —
the prose exists so a non-Python implementer has something to follow, not
to be a second, driftable authority.

## 3. The op envelope

Every op crossing the wire (pushed, pulled, or served by a Transport B
peer) is one JSON object with exactly these fields:

| Field | Type | Signed (§4) | Nullable | Notes |
|---|---|---|---|---|
| `op_id` | string | yes | no | uuid4, canonical lowercase hyphenated form (`8-4-4-4-12` hex). The global idempotency key. |
| `machine_id` | string | yes | no | uuid4, same form. The authoring machine. |
| `machine_seq` | integer | yes | no | ≥ 1. Gap-free per `machine_id`, assigned by that machine, never by a receiver. |
| `lamport` | integer | yes | no | ≥ 1. See `sync.md` "Ordering" for how a sender computes it; this document only fixes its wire type and that it is part of the signed tuple. |
| `class` | string | yes | no | One of `memory`, `filemap`, `belief`, `pending`, `skill`, `session`, `transcript`, `tabset`, `worktree` today. A server MUST NOT reject an op solely because `class` holds a value outside this list — see §8, "the hub never interprets ops": a future class must cross an unmodified hub. |
| `op` | string | yes | no | Per-class verb, e.g. `add`, `insert`, `reinforce`. Same forward-compatibility rule as `class`. |
| `project_key` | string or `null` | yes | yes | `null` means user scope. See `sync.md` prerequisite (a) for how a client derives it; this document only fixes that `null` (not `""` or omission) is the wire spelling for "no project". |
| `payload` | object | yes | no | Per-class shape, Appendix A (informative here; normative in `sync.md`). Scrubbed (`scrub_secrets`) before it is ever written to a local `sync_ops` row, let alone signed. |
| `mac` | string or `null` | — (this field *is* the signature) | yes | Lowercase hex, exactly 64 characters, when present: `HMAC-SHA256(...).hexdigest()`. `null` is a legal wire value — see §5.3 — but a v1 client MUST attempt to compute a real `mac` whenever `LORE_SYNC_HMAC_KEY` is configured locally, and MUST NOT push `null` merely because it is easier. |
| `created` | string | no | no | RFC 3339, UTC, trailing `Z` (e.g. `2026-09-14T17:30:09Z`). Fractional seconds MAY be present. Display only — never compared, never part of ordering (`sync.md` "Ordering"), and explicitly excluded from the signed tuple so a `created` clock's precision can never break verification. |

Two fields exist only in a *pull response* item, never in a pushed op and
never signed:

| Field | Type | Notes |
|---|---|---|
| `hub_seq` | integer | This server's own paging position for this op. See §6.2 and §7 — a Transport B peer emits this field too, under this name, populated from its own local monotonic counter; the name is a wire-format convention shared by both transports, not a claim that a hub is involved. |

No other fields are part of this contract. A sender MAY include
additional top-level fields for forward compatibility (a future minor
revision might add one); **a receiver MUST ignore any field it does not
recognise**, on both the op object and every HTTP response body in this
document. A field addition alone is never a breaking change; removing or
retyping one of the fields above is.

## 4. What `mac` signs

Build the JSON value:

```
[op_id, machine_id, machine_seq, lamport, class, op, project_key, payload]
```

— a JSON **array**, these 8 elements, in exactly this order, taken
straight from the op's own fields. (An array, not an object: this sidesteps
member-order ambiguity for the tuple itself. `payload`, which *is* an
object, still needs §2's member-sorting rule applied recursively within
it.)

Encode it with the canonical JSON algorithm of §2. Call the result
`canonical_bytes`.

```
mac = HMAC-SHA256(key = UTF-8 bytes of LORE_SYNC_HMAC_KEY,
                   message = canonical_bytes).hexdigest()
```

`hexdigest()` — lowercase hex, 64 characters — is the wire form of `mac`.
`hmac` and `hashlib` are stdlib; this promise costs LORE nothing (see
`docs/manual.md` and `tests/test_packaging.py`, the empty dependency
list).

`LORE_SYNC_HMAC_KEY` is a shared secret configured identically (the exact
same string) on every machine of one account. It is UTF-8 encoded to
bytes with no other transformation — no hashing, no KDF, no trimming of
whitespace. **The hub never holds this key, never sees it in any request,
and never computes or checks a `mac`.** Ops are authenticated
machine-to-machine; the hub is a courier (§8).

### 4.1 Worked example

`tests/fixtures/sync_protocol/memory_add.json` fixes this op:

```json
{
  "op_id": "8f14e45f-ceea-467e-9a9e-4b0b8b4f1a10",
  "machine_id": "1b1e1fbf-9c1e-4f0f-9c1e-9c1e9c1e9c1e",
  "machine_seq": 1,
  "lamport": 101,
  "class": "memory",
  "op": "add",
  "project_key": null,
  "payload": {"text": "Prefers concise commit messages.", "via": "interactive", "writer": "approved"},
  "created": "2026-09-14T17:30:09Z"
}
```

under the **test vector key** used by every fixture in this repository —
fake, fixed, and never to be used for anything but these tests:

```
LORE_SYNC_HMAC_KEY = lore-sync-protocol-test-key-DO-NOT-USE-IN-PRODUCTION
```

`canonical_bytes` (UTF-8 text; the fixture carries this as hex):

```
["8f14e45f-ceea-467e-9a9e-4b0b8b4f1a10","1b1e1fbf-9c1e-4f0f-9c1e-9c1e9c1e9c1e",1,101,"memory","add",null,{"text":"Prefers concise commit messages.","via":"interactive","writer":"approved"}]
```

```
mac = 52611e5972dd512c787b0fd61b6c463daf1c6589d4d38d5e1e587c14e600f0a8
```

Note `null` (not `"null"` or the field simply missing) for `project_key`,
no space after any `,` or `:`, and object keys `text` / `via` / `writer`
in that (already alphabetical) order — `payload` was constructed in that
order here, but the rule holds even when a payload is built in a
different order; §2.3 sorts it regardless of construction order.

## 5. MAC verification (receiver behaviour)

This section binds any machine applying a pulled or peer-fetched op —
the apply engine is a separate implementation track, but the contract it
must honour is fixed here, not left to that track to invent.

### 5.1 The check

On receiving an op, a receiver:

1. Rebuilds the 8-element array of §4 from the op's own fields as
   received (not from any local copy).
2. Encodes it with §2 to get `canonical_bytes`.
3. Computes `expected = HMAC-SHA256(LORE_SYNC_HMAC_KEY, canonical_bytes).hexdigest()`.
4. Compares `expected` to the op's `mac` field using a constant-time
   comparison (`hmac.compare_digest`, not `==`).

### 5.2 On failure

An op whose `mac` is **absent (`null`) or does not match** MUST NOT be
applied. It MUST instead be staged as a pending proposal — `kind: sync`,
tagged `unverified` — through the same pending mechanism as every other
gated write (`docs/write-gate.md`). It is visible in `/lore:pending`,
approvable, rejectable, and steers nothing on its own. `null` and "wrong"
are the same failure for this purpose: a receiver MUST NOT special-case a
missing `mac` as "trust it, nothing to check" — that is exactly the
downgrade a hub or a malicious peer could induce by stripping the field,
and the contract exists to make that induce a pending review, not a
silent apply.

**Verification comes before the receiver commits anything to the op.** A
receiver MUST check the `mac` *before* it claims the op's `(machine_id,
machine_seq)` slot in its own log and *before* it advances its Lamport
clock past the op's. An op that does not verify is one anybody could have
written, so it earns a place in the pending pile and nowhere else: in
particular it MUST NOT occupy the slot the genuine author's op needs
(which would make that op arrive as a duplicate and be dropped) and it
MUST NOT move the receiver's clock (which would let anyone set it).

A receiver still STORES the op it could not verify, and still relays it
onward — the courier property of §7 does not exempt mail this machine
cannot read. It stores it in a way that leaves the slot free: `lore`
records it in `sync_ops` with `applied = 2` and excludes exactly that
state from its `(machine_id, machine_seq)` unique index, so a verified op
for the same slot can land beside it afterwards. When a human later
approves the staged proposal and the slot has since been filled by a
different `op_id`, the approval is refused rather than applied beside it:
two ops cannot both hold one machine's `machine_seq` (§6.2), and the one
already applied is the one that verified.

### 5.5 Ops a receiver accepts but does not apply

Verification decides whether an op is *this account's*; it does not
decide whether this build can act on it. Three outcomes are neither
"applied" nor "staged", and a receiver MUST keep them apart from both:

- **Unknown `class` or `op`.** Recorded, relayed, never applied, and —
  this is the part that matters — never picked up by the dependency-retry
  path either. A receiver MUST NOT record an unrecognised op in the same
  state it records one that is merely waiting on a dependency: a retry
  pass that does not re-verify (it must not; see §5.2) would otherwise
  mark it applied without ever having applied it. §8's
  forward-compatibility rule is what it stays recorded FOR.
- **Structurally invalid.** An op missing a signed field of §3, carrying
  one of the wrong JSON type, or carrying a `machine_seq`/`lamport`
  outside `0 .. 2**63 - 1` is not an op: it cannot be verified and cannot
  be ordered. It is counted and dropped, with the reason reported. §3
  says "integer"; the bound is what an implementation MUST enforce to
  keep one wire value from being unstorable.
- **Refused or failed on apply.** A verified op of a known class whose
  payload the applier will not act on (a path that would escape the
  store's own directory, a batch of rows that are not rows) or which
  raises. A receiver MUST contain that to the one op: the rest of the
  page MUST still apply, and the op MUST reach a terminal state that a
  status command reports. A page that aborts is a page that is re-fetched
  and aborts again on the next pull, forever.

A receiver MAY additionally decline a whole class it has been configured
not to hold (`LORE_SYNC_CLASSES`). A declined class is counted and
dropped, not staged: an operator who switched a class off has already
answered the question staging would ask.

### 5.3 When the receiver has no key configured

A machine that has not set `LORE_SYNC_HMAC_KEY` locally cannot perform
the check in §5.1 at all. It MUST treat **every** incoming op as unverified
under §5.2 — stage, never apply — rather than skip the check and apply
directly because no key is present to check against. "No key configured"
and "key configured but verification failed" have the same outcome for
the receiver; only `lore doctor` distinguishes them for a human (missing
key vs. bad mac are different diagnoses, same containment).

### 5.4 Why `mac` may legally be `null` on the wire at all

A sender that has not configured `LORE_SYNC_HMAC_KEY` (a genuinely fresh
machine, before the operator has provisioned the shared secret) can still
push and pull — the hub does not require or inspect `mac` (§8) — but every
other machine that *does* have the key will stage 100% of that sender's
ops as unverified until the key is provisioned there too. This is the
intended failure mode: sync degrades to "visible but not auto-applied,"
never to "silently trusted."

## 6. Transport A: the hub

All endpoints are under `/v1`. Every request and response body is JSON,
`Content-Type: application/json; charset=utf-8`. Every non-2xx response
body is:

```json
{"error": "<snake_case_code>", "message": "<human-readable, non-normative>"}
```

possibly with additional fields, noted per status code below. `error` is
normative and MUST be one of the exact strings given; `message` is for a
human and a server MAY phrase it however it likes.

**A server MUST NOT answer any of these endpoints with a redirect**, and a
client MUST NOT follow one to another origin. `urllib`-shaped clients
re-send the original headers to whatever `Location` names, so following a
cross-origin redirect hands the machine's bearer token to whoever issued
it; a client refuses it and reports the hub as misconfigured. A
same-origin redirect, if a client follows one at all, MUST be followed
without the credential.

**The closed set of `error` strings.** `error` MUST be one of the exact
strings this document gives. Four of them describe conditions v1 did not:

| Status | `error` | Raised when |
|---|---|---|
| 409 | `op_id_conflict` | an `op_id` the account already holds is claimed by a different `machine_id` or a different `machine_seq` (§6.2, §9) |
| 413 | `payload_too_large` | the request body is over the server's cap (§6.2, §6.6) |
| 503 | `busy` | another push holds this account's lock, or the server's pool is saturated. A client MUST retry it — treating it as fatal stops that machine syncing |
| 500 | `internal_error` | anything unforeseen, in the body shape above rather than as a text/plain traceback |

A client that does not recognise a string still handles the status class:
a 409 is never retried unchanged, a 413 is split and re-sent, a 503 is
retried. `lore` retries `busy` three times (0.2 s, 0.5 s, 1.0 s) and then
reports it as "still busy", which is never a failed push and never a
cursor that advanced.

**A client MUST bound what it reads.** The pull that runs at session
start is detached and unattended, so an answer of unbounded size is read,
decoded and parsed before a single byte of it has been verified. A client
caps the whole response and refuses past it (`lore`: 32 MiB), and caps
each op's `payload` independently before that op can be applied OR staged
(`lore`: 1 MiB) — staging writes the op's own bytes to disk, so the cap
that matters is the one in front of both.

### 6.1 Authentication

`Authorization: Bearer <token>` (bearer mode) or the Tailscale identity
headers `Tailscale-User-Login` / `Tailscale-User-Name` (tailscale mode),
per `LORE_HUB_AUTH` on the server (`sync.md` "Auth: two modes, one
switch"). Every endpoint below except `GET /health` requires one of the
two.

- **401** `unauthenticated` — no credential present, an unparseable
  bearer token, or a Tailscale identity header presented on the public
  listener (MUST be refused there even if otherwise well-formed — trusted
  only when it arrived on the loopback listener `tailscale serve`
  forwards to).
- **401** `token_revoked` — a syntactically valid bearer token whose
  `tokens.revoked` is set.
- **403** `forbidden` — a valid credential lacking the scope the endpoint
  needs (`push` for `POST /ops`, `pull` for `GET /ops` and `GET
  /snapshot`), a Tailscale login not on the hub's allow-list, or — on
  `POST /ops` in bearer mode — a batch whose top-level `machine_id` is not
  the machine the token was issued for. A server MAY bind a token to one
  machine (`lore-hub` does, and answers this 403); a client MUST NOT retry
  into it, since no retry changes which machine its credential names.

### 6.2 `POST /ops` — push

Request:

```json
{
  "machine_id": "1b1e1fbf-9c1e-4f0f-9c1e-9c1e9c1e9c1e",
  "ops": [
    {"op_id": "...", "machine_seq": 812, "lamport": 4410, "class": "memory",
     "op": "add", "project_key": null, "payload": {...},
     "mac": "...", "created": "2026-09-14T17:30:09Z"}
  ]
}
```

Every op in `ops` MUST omit `machine_id` at the per-op level in this
request body — it is not shown above because the top-level `machine_id`
applies to the whole batch. (Contrast with §4: the *signed* tuple still
includes each op's own `machine_id`; the server reconstructs it from the
request's top-level field for every op in the batch when recomputing
nothing — the hub never recomputes a `mac`, §8 — but a server MUST verify
structurally that a request never claims two different machines in one
batch, which this shape prevents by construction.)

- **200** — batch processed (see machine_seq handling below for what
  "processed" can mean short of full acceptance):
  ```json
  {"accepted": 17, "duplicate": 3, "hub_seq_max": 90211}
  ```
  `accepted` — ops newly stored by this call. `duplicate` — ops whose
  `op_id` already existed for this account **at the same `machine_id` and
  the same `machine_seq`**; that triple is what makes a retry a retry. A
  collision on any other pairing is 409 `op_id_conflict` below, not a
  duplicate (§9; the hub still does not compare payload bytes).
  `hub_seq_max` —
  the highest `hub_seq` now on the server for this account after this
  call (or the previous value if nothing new landed; `null` only if the
  account has zero ops even after this push, which cannot happen given a
  non-empty `ops` array unless every one of them failed structurally, in
  which case the response is 400, not 200).

- **400** `bad_request` — malformed JSON, `ops` empty or absent, a field
  from §3 missing or the wrong JSON type, or an op's `machine_id` implied
  by context does not match the request's top-level `machine_id`. A
  server MUST NOT return 400 solely because `class` or `op` holds a value
  it does not recognise (§3, §8) — only structural violations of the
  shapes in §3 are 400.

  A value of the right JSON type but out of range is also 400: a
  `machine_seq` or `lamport` outside `0 … 2^63-1`, an `ops` array longer
  than the server's cap, JSON nested deeper than the server's cap, or a
  body that is not UTF-8. So is a `created` without a UTC offset — §3
  already fixes it as RFC 3339 UTC with a trailing `Z`, and this is the
  one place a v1 client's 200 can become a 400. `created` is outside the
  signed tuple (§4), so a client holding older rows normalises the field
  on the way out rather than being unable to push them at all.

- **409** `machine_seq_gap` or `machine_seq_conflict` — see below.

- **409** `op_id_conflict` — an `op_id` this account already holds,
  claimed by a different `machine_id` or a different `machine_seq`.
  Unlike the two above, this rolls the **whole push** back: the body
  carries `accepted: 0, duplicate: 0` plus `op_id`, `machine_id`,
  `machine_seq`, `stored_machine_id` and `stored_machine_seq`, and
  nothing advanced, so the corrected batch is re-sent whole. Never
  retried unchanged: `op_id` is the one identifier every idempotence rule
  here rests on (§9), and two ops sharing one is a bug, not a race.

- **413** `payload_too_large` — the body is over the server's cap. A
  client splits the batch and re-sends; `GET /health` publishes the caps
  (§6.6) so it need not discover them this way.

- **503** `busy` — retried by the client (§6). Nothing was stored and no
  cursor may advance.

Ops are processed **in array order, independently per `machine_id`** (a
batch MAY legitimately contain ops from more than one machine only when a
future multi-machine batching mode is added; v1 clients send one
`machine_id` per request as shown above, but a server MUST NOT assume
that and must key its gap tracking by each op's own `machine_id`).
For a given `machine_id`, let `expected` be (highest `machine_seq` this
account has ever accepted for that `machine_id`) + 1, starting at 1 for a
machine the server has never seen. Walking the batch in order for that
machine_id:

- `machine_seq == expected` → store it (or count as `duplicate` if this
  exact `op_id` is already stored — a page resend), `expected += 1`,
  continue.
- `machine_seq < expected` and the `op_id` at that already-filled slot
  matches this op's `op_id` → `duplicate`, continue (idempotent resend).
- `machine_seq < expected` and the `op_id` differs from what is stored at
  that slot → stop processing this machine_id's remaining ops and
  respond **409** `machine_seq_conflict`:
  ```json
  {"error": "machine_seq_conflict", "machine_id": "...", "expected": 813,
   "got": 811, "accepted": 4, "duplicate": 1}
  ```
  (two different ops claiming one `machine_seq` slot from one machine —
  a client-side bug, never expected in normal operation, since
  `machine_seq` is a local monotonically-incrementing counter the author
  alone advances).
- `machine_seq > expected` (a gap) → stop processing this machine_id's
  remaining ops and respond **409** `machine_seq_gap`:
  ```json
  {"error": "machine_seq_gap", "machine_id": "...", "expected": 813,
   "got": 815, "accepted": 4, "duplicate": 1}
  ```

`accepted` and `duplicate` in a 409 body count only what was processed
for the *affected* `machine_id` before the gap/conflict was hit. Ops for
other `machine_id`s in the same batch (multi-machine batching, if ever
used) are processed to completion regardless — one machine's gap MUST
NOT block another machine's ops in the same request. A client recovering
from 409 re-sends starting at its own `pushed_seq + 1` (`sync_peers`);
re-sending already-accepted ops is safe (they come back `duplicate`).

This is the same shape `sync.md`'s Failure modes table describes: "a gap
means a lost op, and a lost op means a store that is no longer a function
of its log" — the 409 exists to make that loud immediately, not silently
downstream at apply time.

### 6.3 `GET /ops?since=<cursor>&limit=<n>&exclude=<machine_id>` — pull

`since` — integer, the highest `hub_seq` the client has already fully
drained and applied (§6.4); `0` or omitted means "from the beginning."
`limit` — integer > 0; a server MAY clamp a larger request down to its
own maximum page size silently (not an error) — a client detects this
only by `next` being non-null with fewer than `limit` ops returned, which
is indistinguishable from "the server just didn't have that many yet" and
MUST be handled the same way (keep paging). `exclude` — optional, a
single `machine_id` whose ops are omitted from the `ops` array of the
response. Filtering by `exclude` does not change what `since`/`next`
mean: both refer to positions in the account's full, unfiltered stream,
so a client that starts excluding or stops excluding between calls never
produces a gap.

- **200**:
  ```json
  {"ops": [{"op_id": "...", "machine_id": "...", "machine_seq": 812,
            "lamport": 4410, "class": "memory", "op": "add",
            "project_key": null, "payload": {...}, "mac": "...",
            "created": "2026-09-14T17:30:09Z", "hub_seq": 90212}],
   "next": 90712}
  ```
  `next` is the `hub_seq` to pass as `since` for the following call, or
  `null` when this page is the end of what the server currently has (an
  empty `ops` array also carries `next: null` unless a later, larger page
  exists — which cannot happen since the server is asked in `hub_seq`
  order and a gap-free store has no "later" page smaller than an earlier
  one). Every item in `ops` carries every field of §3 plus `hub_seq`.

- **400** `bad_request` — `since` or `limit` not a non-negative /
  positive integer respectively.

### 6.4 Paging contract — merge order is never page order

A page's `ops` array is in `hub_seq` order (insertion order on this
server), **not** merge order. A client:

1. Repeats `GET /ops` with `since = <previous next>` until a response
   comes back with `next: null` — this is "draining."
2. MUST NOT apply anything before the drain completes. It concatenates
   every page's `ops` array in the order the pages were fetched.
3. Sorts the full concatenation ascending by `(lamport, machine_id,
   machine_seq)` — string comparison on `machine_id`, numeric on the
   other two — never by `hub_seq`.
4. Applies in that sorted order (§5 governs each op's MAC check as it is
   applied).
5. Only after every op in the drain has been applied does it advance its
   local `sync_peers.pulled_cursor` to the final `next` value it saw
   (which was `null`, so in practice it records the last non-null `next`
   it received, or leaves the cursor where it can resume the drain). If
   the process is interrupted mid-drain, the cursor MUST NOT have
   advanced past where it started — the next pull re-drains from the
   original `since`, and every op in it is either a fresh application or
   a no-op through §9's idempotence, never lost.

This is what `sync.md` means by "the client sorts a drained pull by
`(lamport, machine_id, machine_seq)` before applying," made precise about
*when* (after a full drain, never per-page) and what "advancing the
cursor" is allowed to mean (only after the sorted apply succeeds).

### 6.5 `GET /snapshot` — reserved

Not implemented in v1. A v1 server MUST respond:

- **501** `not_implemented`

A server MUST NOT repurpose this path for anything else while this
document says "reserved." A v1 client MUST NOT call it; bootstrap in v1
is `GET /ops?since=0` drained to completion (`lore sync bootstrap`,
`sync.md` "Open decisions" §6).

### 6.6 `GET /health`

No authentication required (this is a liveness/monitoring endpoint; it
MUST work for a caller with no account at all).

- **200**: `{"ok": true, "version": "...", "hub_seq_max": 90712,
  "limits": {"max_ops_per_push": 1000, "max_body_bytes": 4194304}}` —
  `hub_seq_max` here is the server's global maximum across all accounts,
  for operational visibility, not scoped to any one caller. `limits` is
  OPTIONAL and carries the caps §6.2's 400 and §6.6's 413 enforce, so a
  client can size its batches from them instead of discovering them by
  being refused; a client that does not see it falls back to its own
  conservative defaults (`lore`: the same two numbers).

### 6.7 `GET /whoami`

Authenticated (§6.1).

- **200**: `{"account": "...", "machine_id": "...", "auth": "token"}` or
  `{"account": "...", "machine_id": "...", "auth": "tailscale"}`.
  `machine_id` is resolved from the bearer token's own record (bearer
  mode) or is absent/`null` in tailscale mode until that machine has
  pushed at least one op carrying it (a Tailscale login is not itself a
  `machine_id`) — a server MUST document which of these two it does; both
  are legal, and `lore doctor` handles a `null` machine_id in the
  response by reporting "not yet seen."
- **401** — no credential, as §6.1.

## 7. Transport B: the direct peer

A peer (`lore sync serve`) implements **only the pull side** of this
contract: `GET /ops`, identical request parameters, response shape,
status codes, and the `hub_seq` field name and semantics (§3), except
that its `hub_seq` is populated from the peer's own local monotonic
position (its `sync_ops.seq`, or any other value the peer keeps
increasing — opaque to the puller either way) rather than a Postgres
`BIGSERIAL`. A puller treats it exactly as it treats a hub's `hub_seq`:
an opaque paging cursor, echoed back verbatim as `since` on the next
call, never used for merge order (§6.4 applies unchanged).

A peer:

- MUST implement `GET /ops` per §6.3–§6.4.
- MAY omit `POST /ops`, `GET /snapshot`, `GET /health`, `GET /whoami`.
  There is no "push to a peer" in this design — both directions of a
  laptop↔workstation sync happen as each side *pulls* from the other
  (`sync.md` Transport B).
- Authenticates by the Tailscale identity headers only (§6.1's tailscale
  mode), behind its own `tailscale serve`, with the same public-listener
  refusal rule. Bearer-token auth is not part of Transport B — a peer has
  no account/token model, only the tailnet's own identity.
- **States what that boundary is.** An identity header is trusted on the
  loopback listener because `tailscale serve` is supposed to be the only
  thing that can reach it, and nothing enforces that: any process running
  as the same user can connect to loopback and write the header itself,
  and can read the allow-list out of the environment to pick a login that
  is on it. **Loopback trust is same-user trust.** That is a defensible
  boundary — a process running as that user can read the store directly
  and skip the listener — but it is not the boundary "authenticated"
  suggests, so a peer MUST NOT imply a stronger one: its startup output
  and its `GET /whoami` say which mode is in force and what it rests on.
- MAY additionally require a shared secret as `Authorization: Bearer
  <secret>` on every authenticated request, in addition to the identity
  header (`lore`: `LORE_SYNC_PEER_SECRET`, set on the listener and on
  every machine that pulls from it). This is the one credential here that
  a co-resident process does not already have. It is orthogonal to §5:
  the secret says who may READ this machine's log, the `mac` says whose
  ops may be APPLIED, and neither substitutes for the other. Unset is the
  default and changes nothing on the wire.
- Signs and verifies ops exactly as §2–§5 describe, with no
  transport-specific variation. This is the concrete meaning of "the
  wire format must be identical for both transports": an op pulled from
  a peer and an op pulled from the hub are indistinguishable once
  `hub_seq` is stripped off, and §5's MAC check does not know or care
  which transport delivered the bytes.

## 8. What a server MAY vary, and what it MUST NOT

**MUST NOT vary** (this is the interoperability surface):

- The 8-field signed tuple, its fixed order, and the canonical JSON
  algorithm of §2.
- The `mac` construction: HMAC-SHA256, lowercase hex, key = UTF-8 bytes
  of the shared secret.
- That `class` and `op` are opaque strings the hub does not validate
  against an enum (§3, §6.2) — forward compatibility for new classes
  requires this.
- Endpoint paths, methods, and the JSON field names/types fixed in §6–§7.
- The status codes and `error` strings fixed in §6.1–§6.5: a server MUST
  NOT repurpose or omit one this document defines. It MAY answer a
  condition this document does not describe with an additional code and
  `error` string, which a client handles by status class until the
  contract takes it up.
- `op_id` as the sole idempotency key, and the duplicate semantics of §9.
- `machine_seq` gap/conflict detection blocking only the affected
  `machine_id`'s remaining ops in a batch (§6.2).
- Merge order as `(lamport, machine_id, machine_seq)`, never `hub_seq`
  (§6.4) — a hub swap or loss MUST NOT change history's order.
- **That the hub never verifies a `mac`, never requests
  `LORE_SYNC_HMAC_KEY`, and never rejects an op for a missing or bad
  `mac`.** Verification is exclusively a receiving machine's job (§5).
  The moment a hub starts checking MACs it also has to hold the key, and
  holding the key is the one thing this design refuses to let it do.
- That the hub never renders, merges, or otherwise interprets a payload
  (`sync.md`: "The hub never interprets ops").

**MAY vary** (implementation freedom):

- Storage engine, indexing, and internal transaction boundaries for
  `POST /ops`, as long as the client-visible `accepted`/`duplicate`/409
  semantics of §6.2 hold.
- Default and maximum `limit` for `GET /ops`, as long as paging (§6.4)
  still terminates correctly.
- Rate limiting, additional request headers, request size limits (a
  server MAY respond 413 for an oversized push; this contract does not
  fix a size, and a server enforcing one SHOULD surface it operationally
  — e.g. `GET /health` — rather than only as a surprise 413).
- Additional endpoints outside `/v1` (token administration, metrics) —
  MUST NOT repurpose any path this document defines.
- Additional unrecognised JSON fields on any object (§3) — receivers
  MUST ignore them.
- Logging, deployment topology, TLS termination detail beyond the
  loopback/public listener distinction §6.1 requires for Tailscale mode.
- `created`'s fractional-second precision (§3) — RFC 3339 UTC with `Z` is
  fixed, sub-second digits are not, since the field is unsigned and
  display-only.

## 9. Idempotency

`op_id` is the single idempotency key for the whole system, end to end:

- **At the hub**, a push carrying an `op_id` already stored for the
  account **by the same `machine_id` at the same `machine_seq`** is
  `duplicate`: not re-stored, not re-counted as `accepted`. The hub
  compares that triple and never `payload` bytes (§8: it does not
  interpret ops), so a retry is always a duplicate whatever the payload
  now says. The same `op_id` arriving under a *different* machine or
  `machine_seq` is not a duplicate but 409 `op_id_conflict` (§6.2): the
  practically-impossible colliding uuid4 is refused rather than swallowed,
  because swallowing it would silently pick one of two different ops.
  Preventing the case remains the sender's responsibility (uuid4 generated
  fresh per logical mutation, never reused).
- **At a receiver applying a pulled op**, `op_id` already applied locally
  is a no-op at the store layer, before any per-class merge rule in
  `sync.md` even runs. Every per-class verb is additionally idempotent at
  the domain level (`sync.md`, "Idempotence"), so replaying the same
  op twice through the *domain* logic, not just the store guard, is also
  a no-op — belt and braces, and the reason `sync.md`'s
  `test_store_is_a_function_of_its_log` can replay a whole log onto an
  empty store and call that bootstrapping.
- **A partial push retry** (client resends a whole page after a timeout,
  not knowing how much the server actually received) is therefore always
  safe: every op the server already stored comes back `duplicate`; every
  op it did not comes back `accepted`; nothing is ever double-applied.

## Appendix A: per-class payload shapes (informative)

Reproduced from `sync.md` for fixture readability. `sync.md` is
authoritative for the merge semantics; this table only fixes the JSON
shape a wire implementation needs to serialize/deserialize without
interpreting it. `via`/`writer` values are the existing provenance
vocabulary (`docs/write-gate.md`): `approved` / `interactive` /
`terminal` / `derived` / `dream`.

**Evolving a payload field.** An op is durable: one written by an older
version of this software MUST still apply, and one written by a newer
version MUST still apply on an older receiver, because both live in the
same log and a bootstrap replays the whole of it. That constrains how a
field may change:

- A field MAY be **widened** — hold more than it used to — when every
  receiver, old and new, already does the right thing with the wider
  value. `skill`/`put`'s `body` was widened this way (ISSUE #73): it
  used to carry the bare skill body and now carries the complete
  `SKILL.md`. Every receiver writes `body` to the skill file verbatim,
  so both readings land correctly and no version flag is needed.
- A field MUST NOT be **renamed**, even alongside the old name, when the
  old name is load-bearing on a receiver. Renaming `body` to `text`
  would have an older receiver read a missing key and write an empty
  `SKILL.md` — silent data loss on a machine that never asked to be
  upgraded.
- A field MAY be **added**; §8 already requires receivers to ignore
  unrecognised fields. An added field MUST NOT be the only way to
  reconstruct what the op describes, or ops written before it become
  unreconstructable.

| Class | Verb | Payload fields |
|---|---|---|
| `memory` / `filemap` | `add` | `{text: string, via: string, writer: string}` |
| `memory` / `filemap` | `remove` | `{key: string}` — `key` is the `entry_key` hash |
| `memory` / `filemap` | `replace` | `{old_key: string, text: string, via: string, writer: string}` |
| `belief` | `insert` | `{uid: string, subject: string, claim: string, confidence: number, via: string, writer: string, created: string, evidence: {session_id: string, project_key: string\|null, note: string}}` |
| `belief` | `reinforce` | `{uid: string, confidence: number, evidence: {...}}` |
| `belief` | `supersede` | `{uid: string, by_uid: string, reason: string}` |
| `belief` | `retract` | `{uid: string}` |
| `belief` | `status` | `{uid: string, status: "active"\|"dormant"}` |
| `belief` | `edge` | `{src_uid: string, dst_uid: string, rel: string, source: string, session_id: string\|null, note: string\|null}` |
| `belief` | `outcome` | `{uid: string, belief_uid: string, event: string, source: string, session_id: string\|null, agent: string\|null, note: string\|null}` |
| `belief` | `dream_reviewed` | `{a_uid: string, b_uid: string}` |
| `pending` | `stage` | `{uid: string, item: object}` |
| `pending` | `resolve` | `{uid: string, status: string}` |
| `skill` | `put` | `{name: string, body: string}` — `body` is the **complete `SKILL.md`**, frontmatter included, so a receiver reproduces the author's file byte for byte by writing it verbatim (ISSUE #73) |
| `skill` | `remove` | `{name: string}` |
| `session` | `upsert` | `{session_id: string, project_key: string\|null, machine_id: string, cwd: string, title: string, first_ts: string, last_ts: string, messages: integer, engine?: string}`; `engine` is informational provenance and defaults to `claude` for older senders. |
| `session` | `msgs` | `{session_id: string, rows: array}` |
| `transcript` (opt-in) | `chunk` | `{session_id: string, from_line: integer, to_line: integer, lines: array<string>}` |
| `tabset` (opt-in) | `put` / `remove` | `{project_key: string, machine_id: string, record: object}` |
| `worktree` (opt-in) | `put` / `remove` | `{project_key: string, machine_id: string, record: object}` |

## Appendix B: test vector key

Every fixture in `tests/fixtures/sync_protocol/` is signed under:

```
LORE_SYNC_HMAC_KEY = lore-sync-protocol-test-key-DO-NOT-USE-IN-PRODUCTION
```

This key is public (it is in this document and in every fixture file). It
MUST NOT be used for anything but reproducing these test vectors.
