# One memory on every machine: sync through an op log and a hub — specification

Status: **accepted 2026-09-16, in implementation**. The identity work this
document stacked on shipped as 0.49.0 (`lore project move`). Owner's decisions
on the open questions at the end: all ten recommendations adopted, with three
amendments. Transport B (Tailscale peer-to-peer) is IN scope rather than
deferred -- sync offers both a credentials hub and a peer-to-peer path, not one
or the other. `docwilde/lore-hub` is PRIVATE for now. The hub is verified
against a locally deployed instance; the Hetzner deployment waits.

## The problem

docwilde runs Claude Code and DOXA on a laptop, a workstation, a Hetzner
box, and in cloud sandboxes where neither `!` nor a tailnet is available.
Each of those has its own `~/.claude/lore`: its own `USER.md`, its own
`state.db`, its own pending pile. A fact approved on the laptop on Monday
is unknown to the workstation on Tuesday, and a belief the workstation's
deriver reached is invisible to a sandbox session an hour later. The
premise of LORE — everything steering the agent is human-approved or
outcome-calibrated — holds per machine and fails across them: the
approval happened, just somewhere else.

The concrete failure that makes this urgent rather than nice-to-have is
identity. A project slug is its root path flattened
(`config.py:272-281`, `re.sub(r"[^A-Za-z0-9]", "-", project_identity_root(cwd))`).
The laptop checks out `~/repo/<owner>/<repo>`; the workstation keeps
`~/Schreibtisch/Ampiric/...`. The same repository is therefore **two
projects** to LORE, with two `MEMORY.md` files, two belief subjects and
two file maps, and no amount of file copying merges them. 0.49.0's
`lore project move` (`relocate.py`) re-files a project after a checkout
moves *on one machine*; it was measured against 1,022 of 1,131 active
beliefs sitting under dead slugs after one reorganisation
(`relocate.py:9-12`). Across machines the same mechanism would have to run
by hand, in both directions, forever. It cannot be the answer.

Measured, for scale: `state.db` is 36 MB, `~/.claude/lore` is 165 MB with
backups, DOXA's own data under `~/.doxa` is 2.7 MB.

## What syncs

Data is what a human or a model produced and would miss on another
machine. Process state is what a running program needs to coordinate with
itself and would be *wrong* on another machine. Only the first kind syncs,
and every class of it is individually switchable, because the cost of
carrying a class is not the same for all of them (a transcript is a
different confidentiality decision than a file-map row).

| Class | Today | Keyed by | Default | What has to change to travel |
|---|---|---|---|---|
| User memory | `USER.md`, flat `- entry` list (`memory.py:44-47`) | entry text; `entry_key` hash in the provenance ledger (`gate.py:280-283`) | **on** | entries become addressable ops (add / remove / replace by hash); file rendered from the merged set |
| Project memory | `projects/<slug>/MEMORY.md` | slug + entry text | **on** | same, plus slug → `project_key` (next section) |
| Provenance ledger | `provenance.json`, keyed `kind:bucket:hash` (`gate.py:276-283`) | entry hash | **on** | travels *inside* the memory/filemap ops as `via`/`writer`; the ledger is rebuilt from ops, never synced as a file |
| File map | `filemap/<slug>.md` (`filemap.py:64-65`) | slug + `path — purpose` | **on** | as project memory |
| Beliefs, evidence, edges, edge assertions, outcomes | `state.db` tables (`store.py:86-172`) | `INTEGER PRIMARY KEY` id; edges `(src, dst, rel)` on those ids | **on** | a `uid` beside the int id (next section); every op addresses beliefs by uid |
| Pending proposals + archive | `pending/<stamp>-<nn>.json` (`gate.py:203-221`, `pending.py:250-260`) | filename stamp, per machine | **on** | a `uid` field in the JSON; stage/resolve ops; local filename stays local |
| Skills | `~/.claude/skills/<name>/SKILL.md` (`config.py:107`) | name | **on** | put/remove ops carrying the body |
| Skill usage | `skill_usage.json` (`deriver.py:564-590`) | skill name | off | per-machine counters; a merge rule (sum, max?) needs its own measurement — deferred |
| Session index | `sessions`, `msg` FTS (`store.py:73-82`) | `session_id` (Claude Code's UUID) | **on** | a `machine_id` column on `sessions`; rows travel as-is (already scrubbed at index time, `store.py:299-304`) |
| Transcripts | `~/.claude/projects/<slug>/<id>.jsonl` (Claude Code's own directory) | session id | off (opt-in) | scrubbed line-by-line chunks; land under `ROOT/transcripts/`, never in Claude Code's directory |
| DOXA tabsets | `~/.doxa/tabsets/<sha256(scope path)[:24]>.json` (`doxa/tabsets.py:20-24, 361-366`) | scope path | off (opt-in) | keyed by `(project_key, machine_id)`; other machines' records visible as remote, restored only on their own machine |
| DOXA worktree metadata | `~/.doxa/worktrees/.meta/<repo>-<short>.json`, `{main_root, branch, base_ref, session_id}` (`doxa/worktrees.py:21-28, 96-109`) | worktree dir name | off (opt-in) | tagged `machine_id`; a record from elsewhere renders as "on workstation", never as an openable path |

**Never synced, because it is not data**: the DOXA peer registry and its
sockets (`$XDG_RUNTIME_DIR/doxa/registry/`, `doxa/peers.py:13-32`),
`worker/*.json` PID files (`deriver.py:1258-1290`), `.refresh/` and
`.midreview/` stamps (`context.py:56, 449`), `tmp/` jobfiles, `logs/`,
`files` (transcript stamps are absolute local paths, `store.py:291-292`),
`backups/`. Each of these describes *this* process on *this* disk.

**Configuration**, all `LORE_*` so `lore config set` writes them into
`settings.json`'s `env` block like every other switch (`bin/lore.py:783-794`):

| Variable | Default | Meaning |
|---|---|---|
| `LORE_SYNC_URL` | unset | hub base URL; unset means sync is off entirely |
| `LORE_SYNC_AUTH` | `token` | `token` (bearer) or `tailscale` (identity header) |
| `LORE_SYNC_TOKEN` | unset | bearer token for this machine; the only mode a sandbox can use |
| `LORE_SYNC_HMAC_KEY` | unset | shared integrity key, set on every machine, never sent (see Security) |
| `LORE_MACHINE_ID` | persisted uuid4 | this machine's identity in the op log; the hostname is a *label*, not the id |
| `LORE_SYNC_CLASSES` | `memory,filemap,beliefs,pending,skills,sessions` | comma list; `transcripts`, `tabsets`, `worktrees`, `skill_usage` are opt-in |
| `LORE_SYNC_PULL_AT_START` | `1` | detached pull at SessionStart |
| `LORE_SYNC_PUSH_AFTER_REVIEW` | `1` | push when the review worker finishes |
| `LORE_SYNC_PEER` | unset | Transport B: a tailnet peer to pull from directly |
| `LORE_DISABLE_SYNC` | unset | stage kill switch, same semantics as `STAGE_SWITCHES` (`config.py:370-389`) |

## Prerequisites — three PRs before any network code

### (a) A project identity that survives the machine

Today the slug is the identity and the identity is a path. The fix is
**not** to change what a slug is: the slug is also the name of the
transcript directory Claude Code writes (`store.py:296-297`: `proj =
jsonl.parent.name`), the `MEMORY.md` directory, the `filemap/` file, and
every `project:<slug>` belief subject. Rewriting all of that on every
machine is the migration 0.49.0 just shipped, run repo-wide, for no local
benefit.

Instead a project gets a second name that is only used on the wire:

```
project_key(cwd) -> str
    origin = `git -C <root> remote get-url origin`, normalised:
      git@github.com:docwilde/LORE.git   -> github.com/docwilde/lore
      https://github.com/docwilde/LORE   -> github.com/docwilde/lore
    no origin, or not a repo              -> the slug itself
```

Lower-cased host and path, scheme and `.git` stripped, the same string
from any checkout of the same remote. A new table maps the two:

```sql
CREATE TABLE IF NOT EXISTS sync_projects(
    project_key TEXT PRIMARY KEY,
    slug        TEXT NOT NULL UNIQUE,
    origin      TEXT,           -- the raw remote URL first seen, for `lore doctor`
    created     TEXT NOT NULL
);
```

`project_slug()` is unchanged. `project_key()` is resolved at op-write
time (the author knows its slug and its key) and at op-apply time (the
receiver looks the key up; a key it has never seen gets a **synthetic
slug** `sync-<key flattened>` and its store is created there). When a
session later starts in a real checkout of that remote, `lore inject`
finds a synthetic slug for its key and runs `lore project move
<synthetic> <slug>` — the 0.49.0 mechanism, re-filing beliefs, evidence,
sessions, pending, memory and file map with cap and provenance carried
(`relocate.py:20-41`). That is the whole reason this plan stacks on #64:
move is the migration primitive, and sync is what makes the migration
happen once instead of by hand on each machine.

Acceptance: two checkouts of one repository at different paths on one
machine produce one key; a repo with no remote produces its slug; the
mapping table survives `lore reset`'s store-only paths; `lore doctor`
prints the key beside the slug.

### (b) Ids that cannot collide

`beliefs.id` is `INTEGER PRIMARY KEY` (`store.py:87-88`), minted by
`lastrowid` (`beliefs.py:146`). Two machines both hold a belief 4711 and
they are different claims. Everything joins on that int: `belief_evidence.
belief_id`, `belief_edges(src, dst)`, `belief_edge_assertions`,
`belief_outcomes.belief_id`, `dream_reviewed(a, b)`, `beliefs.
superseded_by`, `belief_fts.belief_id`, 1,135 lines of `graph.py`, and the
`[4711]` a human reads in every CLI line (`beliefs.py:369`).

**Decision: keep the integer primary key for local joins; add a `uid`.**

```sql
ALTER TABLE beliefs ADD COLUMN uid TEXT;          -- uuid4, UNIQUE index below
CREATE UNIQUE INDEX IF NOT EXISTS beliefs_uid ON beliefs(uid);
ALTER TABLE belief_outcomes ADD COLUMN uid TEXT;  -- append-only ledger rows travel too
CREATE UNIQUE INDEX IF NOT EXISTS belief_outcomes_uid ON belief_outcomes(uid);
```

Same ALTER-inside-except shape as the `last_referenced` and `writer`/`via`
migrations (`store.py:98-115`), and — unlike those — **back-filled**: a
row without a uid cannot travel, and a random uuid4 is not a fabricated
fact about the row, it is a name. Edges and assertions carry no uid of
their own; on the wire they are addressed by `(src_uid, dst_uid, rel)`,
which is what their primary key already means. Replacing the int with the
uuid was considered and rejected: it rewrites every join, every graph
traversal and every CLI line for a property (global uniqueness) that only
the wire needs, and the wire can carry a sidecar.

Pending proposals get a `uid` field in the JSON payload on staging
(`gate.py:203-221` and the deriver's `stage_proposals`). The filename
stays `<stamp>-<nn>.json`: it is a local sort order, and `resolve_ids`
keeps working on it.

Session ids are Claude Code's UUIDs already (`store.py:296`). Memory and
file-map entries have no ids because they *are* their text
(`gate.py:268-272`); the op log addresses them by the provenance ledger's
existing `entry_key` hash, so nothing new is minted for them.

Acceptance: after migration every belief and outcome row has a uid;
`lore belief show 4711` prints exactly what it printed before;
`tests/test_packaging.py` still asserts an empty dependency list.

### (c) Scrubbing at the upload boundary

`scrub.py`'s contract is "every place a transcript or a model's own output
is about to be written to persistent state or re-sent to a model"
(`scrub.py:4-10`). It is honoured where it says: `msg` rows are scrubbed
before insert (`store.py:268, 304, 377`), and the deriver scrubs claims,
evidence, memory proposals, file-map rows and skill bodies
(`deriver.py:1564, 1576, 1784, 1792, 1857-1858, 1879, 1909`). So the
correction to an earlier reading of this code: the FTS index does **not**
hold raw transcript text.

What is *not* scrubbed is what a human typed: `memory_add` from
`/lore:remember` (`memory.py:102-119`), `lore belief add`, and a
gate-staged proposal's `text` (`gate.py:214`). On one machine that is
fine — the user wrote it, the user reads it. On the wire it is a third
place secrets can travel, and the hub is a third disk they can sit on.

The rule: **`sync push` runs `scrub_secrets` over every string field of
every op payload, and a test asserts that no wire payload matches any
`SECRET_PATTERNS` entry.** The scrubber is idempotent on its own output
(`[REDACTED:hex]` contains no forty-hex run), so text scrubbed at ingest
passes through unchanged. Transcript chunks (opt-in) are scrubbed per line
before they leave, with the same "scrub before truncate" ordering
`index_live` learned in 0.31.1 (`store.py:373-377`).

## The core: a local op log

Every mutation LORE makes to a synced class appends one row here, in the
same SQLite transaction as the mutation where the mutation is in SQLite,
and immediately after the file write where it is a file. A machine's
store is then a *function of its op log* — which is the property the
whole design rests on, and the first thing the tests pin.

```sql
CREATE TABLE IF NOT EXISTS sync_ops(
    seq         INTEGER PRIMARY KEY,   -- local append order, never on the wire
    op_id       TEXT NOT NULL UNIQUE,  -- uuid4, the idempotency key everywhere
    machine_id  TEXT NOT NULL,         -- author
    machine_seq INTEGER NOT NULL,      -- author's own counter, gap-free per machine
    lamport     INTEGER NOT NULL,      -- logical clock, see ordering
    class       TEXT NOT NULL,         -- memory|filemap|belief|pending|skill|session|transcript|tabset|worktree
    op          TEXT NOT NULL,         -- per-class verb, below
    project_key TEXT,                  -- NULL for user scope
    payload     TEXT NOT NULL,         -- JSON, scrubbed before it is written here
    mac         TEXT,                  -- HMAC-SHA256 over the canonical bytes, see Security
    created     TEXT NOT NULL,         -- wall clock, DISPLAY ONLY
    applied     INTEGER NOT NULL DEFAULT 0,
    UNIQUE(machine_id, machine_seq)
);
CREATE INDEX IF NOT EXISTS sync_ops_order ON sync_ops(lamport, machine_id, machine_seq);

CREATE TABLE IF NOT EXISTS sync_machine(
    machine_id TEXT NOT NULL,          -- this machine
    label      TEXT,                   -- hostname at first use, cosmetic
    lamport    INTEGER NOT NULL        -- highest logical clock seen
);

CREATE TABLE IF NOT EXISTS sync_peers(
    peer          TEXT PRIMARY KEY,    -- 'hub', or a tailnet node name
    pushed_seq    INTEGER NOT NULL DEFAULT 0,  -- last local seq the peer acknowledged
    pulled_cursor TEXT,                -- opaque, the peer's own position marker
    last_push     TEXT, last_pull TEXT, last_error TEXT
);

CREATE TABLE IF NOT EXISTS sync_belief_aliases(
    uid       TEXT PRIMARY KEY,        -- a remote uid that FOLDED into a local row
    belief_id INTEGER NOT NULL         -- the local row it now means
);
```

**Ordering.** Every op carries a Lamport timestamp: `lamport = max(own,
highest seen) + 1` on write, and the receiver bumps its own clock past
every op it applies. The canonical total order of the whole distributed
log is `(lamport, machine_id, machine_seq)`. It is deterministic, it is
the same on every node whatever route the ops took, and it never reads a
wall clock — `created` is there so a human can read the log, and for
nothing else. A hub may additionally stamp a `hub_seq` for paging, but
paging order is not merge order; the client sorts what it pulled.

**Idempotence.** `op_id` is unique; an op seen twice is a no-op at the
store. Every per-class verb is also idempotent at the *domain* level
(below), so an op that was applied and then re-applied from a re-pulled
page changes nothing. Replaying the whole log onto an empty `ROOT` from
seq 1 is therefore the same as bootstrapping, and it is the test.

**Cursors.** Per peer: the highest local seq the peer has acknowledged
(push) and the peer's own opaque cursor (pull). A push resends from
`pushed_seq + 1`; the peer answers with what it accepted and what it
already had.

### Verbs, per class, and the merge rules

Applying is a pure function of `(local state, op)`, and the rules below
are the whole of it. Where two machines' ops conflict, the rule says what
every node does — the same thing — and what a human is told.

**memory / filemap** (`add {text, via, writer}`, `remove {key}`,
`replace {old_key, text, via, writer}`; `key` = `entry_key` hash).
`add` of a key already present is a no-op (`memory_add` already treats an
exact duplicate as success, `memory.py:113-114`). `remove` of an absent
key is a no-op. `replace` is `remove old_key` then `add text`. Provenance
is recorded from the op's `via`/`writer`, so the ledger on the receiver
says what the author's ledger said. Conflicts:

1. *Same entry replaced on two machines with different wording.* Both
   replaces remove the old key once; both new texts are added. Nothing is
   lost and nothing is auto-chosen; the file now has two entries, and
   `lore sync status` lists the pair under **conflicts** until one is
   removed by hand. A model is not asked to pick — this is exactly the
   write the gate exists to keep human.
2. *Removed on A, replaced on B.* The removal is honoured (the old text is
   gone), and B's new wording lands as an add. The user who deleted did
   not lose the deletion; the user who rewrote did not lose the rewrite.
3. *Over cap after merge.* The merged set is rendered in canonical order;
   entries are written until the cap; the tail that does not fit is
   staged as pending proposals (`kind: memory, action: add, origin:
   sync-overflow`) with a **deterministic** uid (`sha256(op_id)`), so
   every node stages the same proposals and one approval, anywhere,
   resolves them everywhere. The file never exceeds the cap and
   `write_entries`'s refusal path (`memory.py:81-94`) is never hit by
   sync.

**belief** (`insert {uid, subject, claim, confidence, via, writer,
created, evidence{session_id, project_key, note}}`, `reinforce {uid,
confidence, evidence}`, `supersede {uid, by_uid, reason}`, `retract
{uid}`, `status {uid, active|dormant}`, `edge {src_uid, dst_uid, rel,
source, session_id, note}`, `outcome {uid, belief_uid, event, source,
session_id, agent, note}`, `dream_reviewed {a_uid, b_uid}`).
`insert` of a known uid is a no-op. `insert` of an unknown uid whose
`(subject, lower(claim))` matches a local **active** row folds: evidence
is attached, confidence lifted, and `sync_belief_aliases` records that
the remote uid now means the local row — the same rule `belief_insert`
already applies to a restatement (`beliefs.py:129-138`). `supersede` and
`retract` only transition an active row (`belief_supersede`'s guard,
`beliefs.py:338-344`), so the first in canonical order wins and the
second is a no-op; the outcomes ledger keeps both events because it is
append-only by design (`store.py:160-166`). `edge` on uids that resolve
inserts through `edge_insert`, whose per-session assertion table already
makes a restatement a no-op (`store.py:148-158`). An edge whose endpoint
uid is unknown is held in `sync_ops` with `applied = 0` and retried after
the next pull, since ops can arrive out of dependency order across pages.

The competing-dreamer case deserves its own line: two machines that both
run `dream_run` over the same reconciled store will supersede the same
pairs in different directions. The active-only guard makes that
deterministic, not correct. Recommendation under *Open decisions*: one
machine dreams, the others set `LORE_DEFER_DREAM`.

**pending** (`stage {uid, item}`, `resolve {uid, status}`). `stage` of a
known uid is a no-op; `resolve` of an unknown or already-archived uid is
a no-op. Approving on A applies the item on A, which emits the memory or
belief op — so B gets the *effect* through that op and the *archival*
through `resolve`. Approved on A and rejected on B in the same interval:
canonical order decides which archive status wins, but the memory op from
A's apply lands either way, which is the right outcome — an approval is a
write, a rejection is only a tidy.

**skill** (`put {name, body}`, `remove {name}`). Last `put` in canonical
order wins; the losing body is staged as a pending skill proposal rather
than silently overwritten. `remove` is idempotent.

**session** (`upsert {session_id, project_key, machine_id, cwd, title,
first_ts, last_ts, messages}`, `msgs {session_id, rows[]}` in chunks).
A session has exactly one author machine, so there is no conflict: the
author's latest upsert is authoritative, `msgs` replaces by session id the
way `index_sessions` already does (`store.py:299-304`). `sessions` gains a
`machine_id` column so `lore search` can print *on workstation* next to a
hit and `claude -r <id>` is only offered where it would work.

**transcript** (opt-in; `chunk {session_id, from_line, to_line, lines[]}`).
Append-only per session; a chunk already held is a no-op. Written under
`ROOT/transcripts/<project_key>/<session_id>.jsonl`, and `lore session`
learns to read from there when the local `PROJECTS_DIR` has no such file.
Not into Claude Code's own directory: that would make `claude -r` half
work on a transcript whose tool results reference files that are not
here, and it would put LORE in the business of writing a format that
"may change between versions" (`store.py:235-236`).

**tabset / worktree** (opt-in; `put {project_key, machine_id, record}`,
`remove {...}`). Keyed by `(project_key, machine_id)`; a machine only ever
restores its own record and only ever writes its own. There is nothing to
merge, only to show.

## Transport A: the hub

A Docker Compose stack — one API container, one Postgres — that any
machine with the URL and a credential can push to and pull from. It is
always on, so an offline laptop catches up when it returns; it holds
the full log, so a fresh machine bootstraps from one place; and it is
reachable from a cloud sandbox, which no peer-to-peer design is.

### Storage

```sql
CREATE TABLE accounts(
    id       UUID PRIMARY KEY,
    login    TEXT NOT NULL UNIQUE,        -- tailscale login or a chosen name
    created  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE tokens(
    id          UUID PRIMARY KEY,
    account_id  UUID NOT NULL REFERENCES accounts(id),
    machine_id  TEXT NOT NULL,
    token_hash  BYTEA NOT NULL UNIQUE,    -- sha256; the token itself is shown once
    scopes      TEXT[] NOT NULL,          -- {'push','pull'}
    created     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used   TIMESTAMPTZ,
    revoked     TIMESTAMPTZ
);

CREATE TABLE machines(
    account_id  UUID NOT NULL REFERENCES accounts(id),
    machine_id  TEXT NOT NULL,
    label       TEXT,
    last_seen   TIMESTAMPTZ,
    PRIMARY KEY(account_id, machine_id)
);

CREATE TABLE ops(
    hub_seq     BIGSERIAL PRIMARY KEY,    -- paging cursor, NOT merge order
    account_id  UUID NOT NULL REFERENCES accounts(id),
    op_id       UUID NOT NULL UNIQUE,
    machine_id  TEXT NOT NULL,
    machine_seq BIGINT NOT NULL,
    lamport     BIGINT NOT NULL,
    class       TEXT NOT NULL,
    op          TEXT NOT NULL,
    project_key TEXT,
    payload     JSONB NOT NULL,
    mac         TEXT,                     -- opaque to the hub
    created     TIMESTAMPTZ NOT NULL,
    received    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(account_id, machine_id, machine_seq)
);
CREATE INDEX ops_page ON ops(account_id, hub_seq);
CREATE INDEX ops_project ON ops(account_id, project_key, hub_seq);
```

The hub stores ops; it does not interpret them. It never renders a
`USER.md`, never runs the merge, never holds a belief table. That keeps
its code small, its attack surface a byte store, and the merge rules in
exactly one implementation — `lore_core` — where the tests are.

### Endpoints

All under `/v1`, JSON both ways, `Authorization: Bearer <token>` or the
Tailscale identity header (next section).

`POST /ops` — push.
```json
{"machine_id": "…", "ops": [{"op_id": "…", "machine_seq": 812, "lamport": 4410,
  "class": "memory", "op": "add", "project_key": null, "payload": {…},
  "mac": "…", "created": "2026-09-14T17:30:09Z"}]}
→ {"accepted": 17, "duplicate": 3, "hub_seq_max": 90211}
```
Idempotent on `op_id`; a page may be re-sent whole after a timeout. A
`machine_seq` gap (the hub holds 811 and receives 813) is refused with
`409` naming the expected seq: a gap means a lost op, and a lost op means
a store that is no longer a function of its log.

`GET /ops?since=<hub_seq>&limit=500&exclude=<machine_id>` — pull.
```json
→ {"ops": [{…, "hub_seq": 90212}, …], "next": 90712}   // next null when drained
```
The client sorts a drained pull by `(lamport, machine_id, machine_seq)`
before applying.

`GET /snapshot` — reserved. v1 bootstrap is a pull from `since=0`; the
endpoint exists in the spec so a fresh machine has one call to make once
replay time on the real log is measured and found too slow. Not built
until that measurement exists.

`GET /health` → `{"ok": true, "version": "…", "hub_seq_max": 90712}`.
`GET /whoami` → `{"account": "…", "machine_id": "…", "auth": "token|tailscale"}`,
for `lore doctor`.

### Auth: two modes, one switch

`LORE_HUB_AUTH=token|tailscale|both` on the server.

**Bearer token.** Minted by an admin command on the hub (`lore-hub token
new --machine laptop --scopes push,pull`), shown once, stored hashed.
Per machine, so revoking the sandbox's token does not touch the laptop's.
This is the only mode a cloud sandbox can use, and it is the reason the
hub exists at all rather than a pure tailnet design.

**Tailscale identity.** The hub sits behind `tailscale serve`, which
terminates TLS on the tailnet and injects `Tailscale-User-Login` and
`Tailscale-User-Name` on every proxied request. The API trusts those
headers *only* when `LORE_HUB_AUTH` includes `tailscale` and the request
arrived on the loopback listener `tailscale serve` forwards to — never on
the public listener, where anyone can type a header. Identity is then a
tailnet login matched against an allow-list the hub owns, the same shape
DOXA's remote spec already settled on for the daemon
(`doxa/docs/plans/remote.md`, *Adopt telag's model rather than inventing
one*: "no new credential store", "the allow-list is DOXA's own"). At home
no machine holds a token. In a sandbox one does. That is the whole
compromise, stated plainly.

### Where the server lives: `docwilde/lore-hub`

A separate repository. In-repo was considered and rejected on three
grounds. `lore_core` promises stdlib-only and a test asserts the empty
dependency list (`docs/manual.md:233`, `tests/test_packaging.py`);
the server needs a Postgres driver and an ASGI server. `/plugin install
lore` copies the whole tree (`docs/manual.md:237`), so server code in
this repo ships to every plugin user's `~/.claude/plugins` for no reason.
And the two release on different clocks: the hub changes when the wire
format changes, the plugin changes daily. The wire contract lives in
*this* repo as `docs/sync-protocol.md` plus a contract test the hub's CI
runs against; the hub implements it. Stack recommendation under *Open
decisions*.

## Transport B: Tailscale peer-to-peer

The same op log, pulled directly from another machine. Each participating
machine runs `lore sync serve` — a small loopback HTTP listener that
serves `GET /ops` from its own `sync_ops` — behind `tailscale serve`; a
peer pulls with `LORE_SYNC_PEER=workstation`. The identity header is the
auth, exactly as in Transport A's tailscale mode, so nothing new is
designed for it. No Postgres, no server, no token.

Where it fits: the laptop and the workstation, both on, both on the
tailnet. Two machines, one pair, and the ops flow in both directions
whenever both are up.

Where it does not, honestly: a cloud sandbox is not a tailnet node and
cannot pull from anything; a machine that is off holds ops nobody else
can fetch until it is on again; N machines are N² pairs and N cursors
each; and there is no single place to bootstrap a fresh machine from — it
must be told which peer to trust as its starting point, and that peer must
be up. Every one of those is solved by an always-on node holding the full
log — which is a hub with tailscale auth and no Postgres.

**Decision (amended 2026-09-16): build A first, then B on the same seam.** The client's transport is one
class with `push(ops)`, `pull(cursor)`, `whoami()`; the hub client is the
first implementation and a peer client would be the second. Nothing in
the op log, the merge rules or the CLI knows which one it is talking to.
B follows A rather than waiting for a reason to exist, and its cost is one
transport class plus `sync serve`.

## The client

`lore sync` is a new subcommand beside `lore config`, `lore project` and
the others in `bin/lore.py`:

| Command | Does |
|---|---|
| `lore sync status` | peer, auth mode, machine id and label, unpushed ops, last push/pull and error, **conflicts** (rule 1 pairs), classes on/off |
| `lore sync push` | send from `pushed_seq + 1`, page by page |
| `lore sync pull` | fetch since the peer cursor, sort, apply, advance |
| `lore sync` | pull then push |
| `lore sync bootstrap` | on a machine whose `ROOT` is fresh: pull from 0 and apply; refuses on a populated `ROOT` unless `--merge`, which is an ordinary pull |
| `lore sync login <token>` | writes `LORE_SYNC_TOKEN` through the same `settings.json` path as `lore config set` |
| `lore sync classes [+class|-class]` | show or edit `LORE_SYNC_CLASSES` |
| `lore sync serve` | Transport B: serve this machine's op log to a peer (pull side only, loopback by default) |

**Background push.** `worker_run` (`deriver.py:1277-1341`) is the one
place every derived write lands: staged proposals, beliefs, edges,
outcomes, then `dream_run`. A push after its `dream_run` step, guarded by
`LORE_SYNC_PUSH_AFTER_REVIEW`, moves the session's whole yield in one
page while the worker is already detached (`deriver.py:1162-1167`,
`start_new_session=True`). Interactive writes — `/lore:approve`, `/lore:
remember`, `lore belief add` — push at the end of the command, in the
foreground, with a short timeout; a failure prints one line and leaves
the ops for the next push. Nothing is ever lost by a failed push; it is
only late.

**Non-blocking pull at SessionStart.** The inject hook has a 30 s budget
(`hooks/hooks.json:10`) and `cmd_inject` renders the snapshot
synchronously (`context.py:359-383`). A network round trip to Hetzner
through `cloudflared` does not belong inside that. So inject **spawns** a
detached `lore sync pull` the way `_maybe_spawn_midsession_review` spawns
a review (`context.py:463-471`: `Popen`, `start_new_session=True`, every
failure silent) and renders whatever is on disk now. The pull lands
seconds later; `refresh_on_change` (`context.py:83-89`) already
re-injects the snapshot on the next prompt when its bytes changed, so
pulled memory reaches the session on the first prompt after it arrives —
no new mechanism, the existing one does it. Throttled by a `.refresh`-style
stamp so a `resume`/`clear`/`compact` storm does not fan out pulls.

**Never on UserPromptSubmit.** The refresh hook has 15 s
(`hooks/hooks.json:22`) and fires on every prompt. Sync touches it
nowhere; the change-detection already there is the delivery path.

## DOXA

DOXA embeds `lore_core` in-process (`README.md:83`; `doxa/pyproject.toml:48`
pins `lore-core @ git+…@v0.48.2` while the plugin is at 0.49.0). It gets
the op log, the merge and the hub client by bumping that pin — its writes
already go through `belief_insert`, `memory_add` and the pending module,
so they append ops without DOXA code changing. Three things are DOXA's own:

1. **Tabset scope key.** Records are keyed by the scope *path*
   (`doxa/tabsets.py:20-24`); with sync on, the record carries
   `project_key` and `machine_id` alongside `scope_key`, and `tabsets.
   resolve` restores only a record whose `machine_id` is this machine's.
   With sync off nothing about restore changes — `test_tabsets.py` is the
   regression bar.
2. **Worktree records.** The `.meta` sidecar (`doxa/worktrees.py:96-109`)
   gains `machine_id`; the sidebar can list a worktree that exists on the
   workstation as *remote*, never as a path to open. `finalize` ignores
   records not its own.
3. **Status bar.** Sync state (last pull age, unpushed count, a conflict
   flag) beside the worktree and branch — the same *say what is happening*
   rule DOXA's remote spec sets for a remote driver.

DOXA's daemon is a long-lived writer on `state.db`; a pull applying ops
while the daemon holds a write transaction waits on the 30 s busy timeout
(`store.py:47-55`) like every other writer. Apply is one transaction per
page, kept short, and never held across a network call — the lesson
`test_write_lock.py` pins for the dreamer.

## Security

**What the hub holds.** Curated memory, file maps, beliefs and evidence
notes, staged proposals, session titles and scrubbed message text, skill
bodies. Scrubbed of credential *shapes*, not of substance: it says what
you work on, with whom, and what you concluded. Treat the hub's disk as
you would `~/.claude/lore` — full-disk encryption on the volume, Postgres
reachable only from the API container's network, nightly `pg_dump` to a
storage box.

**Scrub guarantee.** Stated under prerequisite (c) and pinned by a test on
the wire payload. It is a shape filter with accepted false negatives
(`scrub.py:112-115`); it is not a reason to sync transcripts by default.

**Tokens.** Per machine, scoped, hashed at rest, shown once, revocable
individually. Rotation is `token new` + `token revoke`; there is no
refresh flow to get wrong. A token grants push and pull for one account —
it is not an admin credential and the hub has no endpoint that would make
it one.

**What a compromised hub exposes, and the one thing that must not
happen.** Read access to everything above is the obvious loss. The
dangerous one is *write*: an attacker who can insert ops into the hub can
insert a memory entry, and a memory entry is injected verbatim into the
context of every future session on every machine. That is a prompt
injection with a persistence layer — the exact failure LORE's write gate
was built to contain (`docs/write-gate.md`).

So ops are **authenticated by the machines, not by the hub**: `mac` is an
HMAC-SHA256 over the canonical JSON of `(op_id, machine_id, machine_seq,
lamport, class, op, project_key, payload)` under `LORE_SYNC_HMAC_KEY`, a
key every machine of one account holds and the hub never sees.
`hmac`/`hashlib` are stdlib, so the promise holds. A receiver applies an
op whose MAC verifies; one whose MAC is missing or wrong is **staged as
a pending proposal tagged `unverified`** — visible, approvable, and
steering nothing until a human says so. The hub becomes a courier that
can drop or delay mail but cannot forge it. Recommendation under *Open
decisions* is to ship this in v1 rather than after.

**Tailscale mode** inherits the tailnet's boundary and the hub's
allow-list; a request with the identity header on the public listener is
refused, and a test asserts it.

## Failure modes and recovery

| Failure | What happens | Recovery |
|---|---|---|
| Hub unreachable | hook-path pull and background push fail **silently** (house rule: a hook never fails over infrastructure); explicit `lore sync` prints the error; ops accumulate; `lore doctor` and `sync status` show the unpushed count and age | nothing to do; next push drains |
| Partial push (timeout mid-page) | the hub accepted some `op_id`s; the client re-sends the page; duplicates are counted, not applied twice | automatic |
| `machine_seq` gap on the hub | `409`; the client has lost or reordered ops locally | `lore sync status` explains; `lore sync bootstrap --merge` re-derives the missing range from the local store — or the store is not a function of its log and that is a bug to report, not paper over |
| Clock skew | none; ordering is Lamport, `created` is display only | — |
| Corrupt local `state.db` | detected by `db_connect`'s own `PRAGMA` failure or a failed apply | restore from `backups/`, then `lore sync pull`; or on a truly dead store `lore sync bootstrap --replace`, which moves the old file aside and replays from 0 |
| Two fresh machines bootstrapping from each other (Transport B, no hub) | both empty: nothing happens; both populated: an ordinary bidirectional pull — beliefs fold by claim, memory dedups by hash, sessions do not collide | `sync status` shows conflicts if any |
| Unverified op (bad MAC) | staged, never applied | `/lore:pending` shows it tagged; reject it |
| Hub data loss | machines hold their own full logs | any machine can `lore sync push --from 0` to re-seed; the hub is a copy, not the original |

## Testing

House rule from `remote.md`: a boundary that tests green and does not hold
is worse than none, so the tests are named for the failure they catch, in
the existing `tests/test_<topic>.py`, stdlib `unittest`, runnable with
`python3 tests/test_sync_merge.py` (the shape of `test_write_lock.py:12`).

**Unit — `tests/test_sync_merge.py`** (each conflict case above is one
named test):

- `test_store_is_a_function_of_its_log` — replay a machine's `sync_ops`
  onto an empty `ROOT`; `USER.md`, every `MEMORY.md`, the belief set by
  uid, edges by `(src_uid, dst_uid, rel)` and the pending pile by uid are
  byte- and set-identical. **The test the design rests on.**
- `test_apply_is_idempotent_under_replay`
- `test_canonical_order_ignores_wall_clock` — ops with `created` years
  apart and reversed lamports apply in lamport order
- `test_same_entry_replaced_on_two_machines_keeps_both_and_flags`
- `test_remove_on_one_and_replace_on_other_keeps_the_new_wording`
- `test_cap_overflow_after_merge_stages_the_tail_identically_on_every_node`
- `test_belief_insert_with_unknown_uid_and_known_claim_folds`
- `test_two_supersedes_of_one_belief_first_in_canonical_order_wins`
- `test_edge_with_unknown_endpoint_waits_and_applies_after_the_endpoint_arrives`
- `test_pending_approved_here_and_rejected_there_still_applies_the_write`
- `test_skill_put_conflict_stages_the_loser`
- `test_wire_payload_carries_no_secret_shape` — every `SECRET_PATTERNS`
  entry against a push page built from a store seeded with each shape
- `test_unverified_op_is_staged_never_applied`
- `test_project_key_is_the_same_from_two_checkouts_and_is_the_slug_without_a_remote`

**Integration — `tests/test_sync_hub.py`**, marked so `make test` skips
it without Docker: a Postgres service container, the hub from
`lore-hub` at a pinned tag, two clients with separate `LORE_ROOT`s and
machine ids. Client A approves a memory entry and derives three beliefs;
client B pulls; A's and B's stores pass the same identity check as the
unit test. Then B replaces the entry, A removes it, both push and pull:
rule 2's outcome on both. Same suite in the hub's own CI against the
plugin at a pinned tag — the contract test both sides run.

**Security assertions** (in the hub repo): the identity header on the
public listener is refused; a revoked token is refused; a `machine_seq`
gap is `409`; and — written the way `remote.md` says such tests must be —
each verified against a deliberately broken build so it cannot pass
vacuously.

## PR sequence

Each PR independently verifiable, each one branch, in this order. LORE
PRs stack on #64 until it merges, then rebase.

| # | Repo | Branch | Lands | Acceptance |
|---|---|---|---|---|
| 1 | LORE | `feat/project-key` | `project_key()`, `sync_projects`, doctor line, synthetic-slug auto-move on inject | two checkouts one key; no remote → slug; `tests/test_project_key.py` green; suite unchanged |
| 2 | LORE | `feat/belief-uid` | `uid` on `beliefs`, `belief_outcomes`; uid in pending JSON; backfill migration | every row has a uid after `db_connect`; all existing tests green; `test_packaging` deps still empty |
| 3 | LORE | `feat/sync-oplog` | `sync_ops`/`sync_machine`/`sync_peers`; every write path appends; `lore sync status` | `test_store_is_a_function_of_its_log` green on a copy of the live store |
| 4 | LORE | `feat/sync-apply` | apply engine, canonical order, all merge rules, HMAC, `sync conflicts` in status | every unit test above green |
| 5 | LORE | `feat/sync-hub-client` | transport class, push/pull/bootstrap/login, detached pull at inject, push after `worker_run`; `docs/sync-protocol.md`; manual + CHANGELOG | integration suite green against a local hub; hook timings unchanged (measure inject before/after) |
| 6 | lore-hub | `main` (new repo) | API, Postgres schema, compose, token admin, tailscale mode, CI with the contract suite | contract suite green; security assertions green against the broken build |
| 7 | lore-hub | `ops/local` | compose brought up locally, `pg_dump` cron, runbook | two `LORE_ROOT`s on this machine converge through the local hub; the Hetzner deployment is a later, separate PR |
| 8 | DOXA | `feat/lore-sync` | pin bump, tabset `project_key`/`machine_id`, worktree `machine_id`, status bar | `test_tabsets.py`/`test_worktrees.py` green; restore unchanged with sync off |
| 9 | LORE | `feat/sync-peer` | Transport B: `sync serve`, peer client | same wire contract as A; two machines converge with no hub |

## Open decisions

1. **Canonical order: Lamport + machine id, or the hub's `hub_seq`?**
   Recommend Lamport. It is transport-agnostic, so Transport B needs no
   second ordering, and it means the hub can be replaced by another hub
   (or lost) without the order of history changing.
2. **HMAC in v1 or v2?** Recommend v1. The op log is the new write path
   into curated memory; shipping it without the containment that the
   existing write path has would be the first LORE release where
   something steers the agent that nobody approved.
3. **Server repo name.** DECIDED: `docwilde/lore-hub`, **private** for now, AGPL like LORE,
   Python 3.12, FastAPI + psycopg 3 + uvicorn, `postgres:17` in compose.
   A stdlib `http.server` hub was considered for symmetry with
   `lore_core` and rejected: the hub is not a library, and a real ASGI
   server is the boring choice for a thing that faces a network.
4. **Token storage on the client.** Recommend `settings.json` `env` via
   `lore config set`, the existing mechanism and the one a sandbox can
   populate from its own environment. A token file was considered; it is
   a second place to look and a second thing `lore teardown` must clean.
5. **Machine id.** Recommend a persisted uuid4 with the hostname as a
   label. Hostnames repeat across reinstalls and sandboxes are all called
   the same thing; an id that collides is an id that merges two
   machines' `machine_seq` streams into one gap-ridden mess.
6. **Bootstrap: replay from 0, or a snapshot?** Recommend replay from 0
   until measured. The log is smaller than the 36 MB `state.db` it
   produces; if replay on the real log is under a minute the snapshot
   endpoint stays reserved.
7. **Who dreams?** Recommend one designated machine (`LORE_SYNC_DREAM_HERE=1`),
   the others on `LORE_DEFER_DREAM`. Reconciliation is one sonnet call
   over the whole active store; running it in three places is three
   times the cost for a result the active-only guard then has to
   arbitrate.
8. **Transcripts: where they land when opted in.** Recommend
   `ROOT/transcripts/`, never `PROJECTS_DIR`, for the reasons under the
   transcript verb.
9. **Default class set.** Recommend `memory,filemap,beliefs,pending,
   skills,sessions` on; `transcripts,tabsets,worktrees,skill_usage`
   opt-in. Everything on by default is the same decision as syncing
   transcripts by default, and that one deserves a deliberate yes.
10. **Does the hub ever interpret ops?** Recommend no, ever. The moment
    it renders a `USER.md` there are two merge implementations, and the
    one without tests is the one on the server.
