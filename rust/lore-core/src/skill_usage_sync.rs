//! Opt-in, measured skill counters. These are ranking hints, never review proof.
//!
//! Wire v1 has one component per (skill, signed envelope author). Within a
//! generation each coordinate merges by max; a higher generation replaces the
//! old component. Distinct authors are summed, never snapshots of fleet totals.
//! A local ledger decrease/deletion rotates that author's generation. Renaming
//! a skill is not inferred: names remain distinct, just as in the local ledger.
//!
//! Pre-existing flat counts have unknown provenance and remain local. Only
//! increases observed after the first baseline are exported. No reason, trail,
//! HEAD, timestamp or reviewer identity crosses this boundary. A shared HMAC
//! authenticates the envelope, not a hardware identity; payload origin MUST
//! equal that envelope's author, including when relayed by another machine.
use crate::{
    config::{valid_id, valid_skill_name, Config},
    files, store, sync_apply, Error, Result,
};
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};

pub const MAX_COUNT: u64 = 1_000_000_000_000;
pub const MAX_GENERATION: u64 = 1_000_000_000;
pub const MAX_ORIGINS: usize = 64;
pub const MAX_SKILLS: usize = 400;
pub const MAX_COMPONENTS: usize = MAX_ORIGINS * MAX_SKILLS;
const FIELDS: [&str; 3] = ["uses", "ok", "fail"];

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Counts {
    uses: u64,
    ok: u64,
    fail: u64,
}
impl Counts {
    fn read(row: &Value) -> Result<Self> {
        if !row.is_object() {
            return Err(Error::InvalidRequest);
        }
        let mut counts = [0; 3];
        for (i, key) in FIELDS.iter().enumerate() {
            counts[i] = row
                .get(*key)
                .map_or(Some(0), Value::as_u64)
                .filter(|n| *n <= MAX_COUNT)
                .ok_or(Error::InvalidRequest)?;
        }
        Ok(Self {
            uses: counts[0],
            ok: counts[1],
            fail: counts[2],
        })
    }
    fn values(self) -> [u64; 3] {
        [self.uses, self.ok, self.fail]
    }
    fn decreases(self, old: Self) -> bool {
        self.values().iter().zip(old.values()).any(|(a, b)| *a < b)
    }
    fn advance(self, current: Self, previous: Self) -> Result<Self> {
        let mut next = [0; 3];
        for (i, ((own, now), old)) in self
            .values()
            .into_iter()
            .zip(current.values())
            .zip(previous.values())
            .enumerate()
        {
            next[i] = own
                .checked_add(now.checked_sub(old).ok_or(Error::Changed)?)
                .filter(|n| *n <= MAX_COUNT)
                .ok_or(Error::TooLarge)?;
        }
        Ok(Self {
            uses: next[0],
            ok: next[1],
            fail: next[2],
        })
    }
    fn max(self, other: Self) -> Self {
        Self {
            uses: self.uses.max(other.uses),
            ok: self.ok.max(other.ok),
            fail: self.fail.max(other.fail),
        }
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Snapshot {
    v: u64,
    name: String,
    origin: String,
    generation: u64,
    uses: u64,
    ok: u64,
    fail: u64,
}
impl Snapshot {
    fn counts(&self) -> Counts {
        Counts {
            uses: self.uses,
            ok: self.ok,
            fail: self.fail,
        }
    }
    fn validate(&self) -> Result<()> {
        if self.v != 1
            || !valid_skill_name(&self.name)
            || !valid_id(&self.origin)
            || !(1..=MAX_GENERATION).contains(&self.generation)
            || self.counts().values().iter().any(|n| *n > MAX_COUNT)
        {
            return Err(Error::InvalidRequest);
        }
        // Do not let scrubbing silently turn a skill or author into a new key.
        let payload = json!(self);
        if crate::scrub::scrub_json(&payload)? != payload {
            return Err(Error::InvalidRequest);
        }
        Ok(())
    }
}
#[derive(Clone)]
struct Local {
    origin: String,
    generation: u64,
    last: Counts,
}
#[derive(Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
struct Binding {
    v: u64,
    origin: String,
    token: String,
}
fn read_binding(cfg: &Config) -> Result<Option<Binding>> {
    let path = cfg.root.join("skill_usage_identity.json");
    if !path.try_exists()? {
        return Ok(None);
    }
    let binding: Binding =
        serde_json::from_slice(&files::read_regular(&path, 512)?).map_err(|_| Error::Changed)?;
    if binding.v != 1
        || !valid_id(&binding.origin)
        || uuid::Uuid::parse_str(&binding.token).is_err()
    {
        return Err(Error::Changed);
    }
    Ok(Some(binding))
}
/// A tiny local continuity witness survives `reset --all` (which retains the
/// flat JSON but drops SQLite). Unknown legacy cannot safely be relabelled as
/// a fresh machine's work or added to relayed copies of its own old history.
/// A missing/mismatched half requires restoring the matching store backup.
fn binding(cfg: &Config, conn: &Connection, create: bool) -> Result<()> {
    let file = read_binding(cfg)?;
    let mut database = None;
    if has_table(conn, "skill_usage_binding")? {
        let mut stmt = conn.prepare("SELECT origin,token FROM skill_usage_binding LIMIT 2")?;
        let mut rows = stmt.query([])?;
        let mut budget = 512;
        if let Some(row) = rows.next()? {
            database = Some(Binding {
                v: 1,
                origin: crate::graph::db_text(row, 0, 128, &mut budget)?,
                token: crate::graph::db_text(row, 1, 36, &mut budget)?,
            });
        }
        if rows.next()?.is_some() {
            return Err(Error::Changed);
        }
    }
    let machine = if has_table(conn, "sync_machine")? {
        conn.query_row("SELECT machine_id FROM sync_machine LIMIT 1", [], |row| {
            row.get::<_, String>(0)
        })
        .optional()?
    } else {
        None
    };
    match (file, database) {
        (Some(file), Some(database))
            if file == database && machine.as_deref() == Some(&file.origin) =>
        {
            Ok(())
        }
        (None, None) if create => {
            let origin = machine.ok_or(Error::Changed)?;
            if !valid_id(&origin) {
                return Err(Error::InvalidRequest);
            }
            let binding = Binding {
                v: 1,
                origin,
                token: uuid::Uuid::new_v4().to_string(),
            };
            // A crash between these writes refuses later sync instead of
            // silently recounting. No local usage mutation has happened yet.
            files::atomic_write(
                &cfg.root.join("skill_usage_identity.json"),
                &serde_json::to_vec(&binding).map_err(|_| Error::Unavailable)?,
            )?;
            conn.execute_batch("CREATE TABLE IF NOT EXISTS skill_usage_binding(origin TEXT NOT NULL,token TEXT NOT NULL)")?;
            conn.execute(
                "INSERT INTO skill_usage_binding(origin,token) VALUES(?,?)",
                params![binding.origin, binding.token],
            )?;
            Ok(())
        }
        (None, None) => Ok(()),
        _ => Err(Error::Changed),
    }
}

pub fn enabled(cfg: &Config) -> bool {
    cfg.sync.enabled && cfg.sync.classes.contains("skill_usage")
}
fn authoring(cfg: &Config) -> bool {
    enabled(cfg) && cfg.sync.key.as_deref().is_some_and(|s| !s.is_empty())
}
fn has_table(conn: &Connection, name: &str) -> Result<bool> {
    Ok(conn
        .query_row(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            [name],
            |_| Ok(()),
        )
        .optional()?
        .is_some())
}
fn schema(conn: &Connection) -> Result<()> {
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS skill_usage_components(
        name TEXT NOT NULL, origin TEXT NOT NULL, generation INTEGER NOT NULL,
        uses INTEGER NOT NULL, ok INTEGER NOT NULL, fail INTEGER NOT NULL,
        PRIMARY KEY(name,origin)) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS skill_usage_components_origin ON skill_usage_components(origin);
        CREATE TABLE IF NOT EXISTS skill_usage_local(
        name TEXT PRIMARY KEY, origin TEXT NOT NULL, generation INTEGER NOT NULL,
        uses INTEGER NOT NULL, ok INTEGER NOT NULL, fail INTEGER NOT NULL) WITHOUT ROWID;",
    )?;
    Ok(())
}
fn db_unsigned(row: &rusqlite::Row<'_>, column: usize, cap: u64) -> Result<u64> {
    u64::try_from(row.get::<_, i64>(column)?)
        .ok()
        .filter(|n| *n <= cap)
        .ok_or(Error::InvalidRequest)
}
fn sql_unsigned(value: u64) -> Result<i64> {
    i64::try_from(value).map_err(|_| Error::TooLarge)
}
fn components(conn: &Connection) -> Result<BTreeMap<(String, String), Snapshot>> {
    if !has_table(conn, "skill_usage_components")? {
        return Ok(BTreeMap::new());
    }
    let mut stmt = conn.prepare(
        "SELECT name,origin,generation,uses,ok,fail FROM skill_usage_components LIMIT 25601",
    )?;
    let mut rows = stmt.query([])?;
    let mut result = BTreeMap::new();
    let mut budget = 8 * 1024 * 1024;
    let mut names = BTreeSet::new();
    let mut origins = BTreeSet::new();
    while let Some(row) = rows.next()? {
        if result.len() == MAX_COMPONENTS {
            return Err(Error::TooLarge);
        }
        let snapshot = Snapshot {
            v: 1,
            name: crate::graph::db_text(row, 0, 128, &mut budget)?,
            origin: crate::graph::db_text(row, 1, 128, &mut budget)?,
            generation: db_unsigned(row, 2, MAX_GENERATION)?,
            uses: db_unsigned(row, 3, MAX_COUNT)?,
            ok: db_unsigned(row, 4, MAX_COUNT)?,
            fail: db_unsigned(row, 5, MAX_COUNT)?,
        };
        snapshot.validate()?;
        names.insert(snapshot.name.clone());
        origins.insert(snapshot.origin.clone());
        if names.len() > MAX_SKILLS || origins.len() > MAX_ORIGINS {
            return Err(Error::TooLarge);
        }
        result.insert((snapshot.name.clone(), snapshot.origin.clone()), snapshot);
    }
    Ok(result)
}
fn locals(conn: &Connection) -> Result<BTreeMap<String, Local>> {
    if !has_table(conn, "skill_usage_local")? {
        return Ok(BTreeMap::new());
    }
    let mut stmt = conn
        .prepare("SELECT name,origin,generation,uses,ok,fail FROM skill_usage_local LIMIT 401")?;
    let mut rows = stmt.query([])?;
    let mut result = BTreeMap::new();
    let mut budget = 256 * 1024;
    while let Some(row) = rows.next()? {
        if result.len() == MAX_SKILLS {
            return Err(Error::TooLarge);
        }
        let name = crate::graph::db_text(row, 0, 128, &mut budget)?;
        let origin = crate::graph::db_text(row, 1, 128, &mut budget)?;
        let local = Local {
            origin,
            generation: db_unsigned(row, 2, MAX_GENERATION)?,
            last: Counts {
                uses: db_unsigned(row, 3, MAX_COUNT)?,
                ok: db_unsigned(row, 4, MAX_COUNT)?,
                fail: db_unsigned(row, 5, MAX_COUNT)?,
            },
        };
        Snapshot {
            v: 1,
            name: name.clone(),
            origin: local.origin.clone(),
            generation: local.generation,
            uses: local.last.uses,
            ok: local.last.ok,
            fail: local.last.fail,
        }
        .validate()?;
        result.insert(name, local);
    }
    Ok(result)
}
fn raw_counts(usage: &Value) -> Result<BTreeMap<String, Counts>> {
    let rows = usage.as_object().ok_or(Error::InvalidRequest)?;
    if rows.len() > MAX_SKILLS {
        return Err(Error::TooLarge);
    }
    rows.iter()
        .map(|(name, row)| {
            if !valid_skill_name(name) {
                return Err(Error::InvalidRequest);
            }
            Ok((name.clone(), Counts::read(row)?))
        })
        .collect()
}
fn put(conn: &Connection, snapshot: &Snapshot) -> Result<()> {
    conn.execute("INSERT INTO skill_usage_components(name,origin,generation,uses,ok,fail) VALUES(?,?,?,?,?,?)
        ON CONFLICT(name,origin) DO UPDATE SET generation=excluded.generation,uses=excluded.uses,ok=excluded.ok,fail=excluded.fail",
        params![snapshot.name,snapshot.origin,sql_unsigned(snapshot.generation)?,sql_unsigned(snapshot.uses)?,sql_unsigned(snapshot.ok)?,sql_unsigned(snapshot.fail)?])?;
    Ok(())
}
fn capacity(all: &BTreeMap<(String, String), Snapshot>, name: &str, origin: &str) -> Result<()> {
    let names: BTreeSet<_> = all
        .keys()
        .map(|(name, _)| name.as_str())
        .chain([name])
        .collect();
    let origins: BTreeSet<_> = all
        .keys()
        .map(|(_, origin)| origin.as_str())
        .chain([origin])
        .collect();
    if names.len() > MAX_SKILLS || origins.len() > MAX_ORIGINS {
        return Err(Error::TooLarge);
    }
    Ok(())
}

/// Receiver-only effect. Reverification is deliberate: neither exact human
/// approval of unsigned traffic nor deferred replay can manufacture an author.
pub fn apply_verified(cfg: &Config, conn: &Connection, op: &Value) -> Result<()> {
    if !enabled(cfg) || !sync_apply::verify_mac(op, cfg.sync.key.as_deref()) {
        return Err(Error::Untrusted);
    }
    if op["class"] != "skill_usage" || op["op"] != "snapshot" || !op["project_key"].is_null() {
        return Err(Error::InvalidRequest);
    }
    let mut incoming: Snapshot =
        serde_json::from_value(op["payload"].clone()).map_err(|_| Error::InvalidRequest)?;
    incoming.validate()?;
    if op["machine_id"].as_str() != Some(incoming.origin.as_str()) {
        return Err(Error::Untrusted);
    }
    binding(cfg, conn, false)?;
    // Indexed point lookup and bounded distinct-key scans, not a decode of
    // the whole component store for each frame in a 512-operation page.
    let mut old = None;
    let mut names: BTreeSet<String> = raw_counts(&super::load_local_usage(cfg)?)?
        .into_keys()
        .collect();
    let mut origins = BTreeSet::new();
    if has_table(conn, "skill_usage_components")? {
        let mut stmt =
            conn.prepare("SELECT name FROM skill_usage_components GROUP BY name LIMIT 401")?;
        let mut rows = stmt.query([])?;
        let mut budget = 128 * (MAX_SKILLS + 1);
        while let Some(row) = rows.next()? {
            names.insert(crate::graph::db_text(row, 0, 128, &mut budget)?);
        }
        let mut stmt =
            conn.prepare("SELECT origin FROM skill_usage_components GROUP BY origin LIMIT 65")?;
        let mut rows = stmt.query([])?;
        let mut budget = 128 * (MAX_ORIGINS + 1);
        while let Some(row) = rows.next()? {
            origins.insert(crate::graph::db_text(row, 0, 128, &mut budget)?);
        }
        let mut stmt = conn.prepare(
            "SELECT generation,uses,ok,fail FROM skill_usage_components WHERE name=? AND origin=?",
        )?;
        let mut rows = stmt.query(params![incoming.name, incoming.origin])?;
        if let Some(row) = rows.next()? {
            old = Some(Snapshot {
                v: 1,
                name: incoming.name.clone(),
                origin: incoming.origin.clone(),
                generation: db_unsigned(row, 0, MAX_GENERATION)?,
                uses: db_unsigned(row, 1, MAX_COUNT)?,
                ok: db_unsigned(row, 2, MAX_COUNT)?,
                fail: db_unsigned(row, 3, MAX_COUNT)?,
            });
        }
    }
    names.insert(incoming.name.clone());
    origins.insert(incoming.origin.clone());
    if names.len() > MAX_SKILLS || origins.len() > MAX_ORIGINS {
        return Err(Error::TooLarge);
    }
    if let Some(old) = old {
        old.validate()?;
        if incoming.generation < old.generation {
            return Ok(());
        }
        if incoming.generation == old.generation {
            let max = incoming.counts().max(old.counts());
            incoming.uses = max.uses;
            incoming.ok = max.ok;
            incoming.fail = max.fail;
        }
    }
    schema(conn)?;
    put(conn, &incoming)
}

/// Display/ranking view. The legacy JSON remains local and unchanged; the
/// private marker keeps imported counts out of the graduated proposal gate.
pub fn visible(cfg: &Config, mut usage: Value) -> Result<Value> {
    if !enabled(cfg) {
        return Ok(usage);
    }
    let local = raw_counts(&usage)?;
    if !cfg.root.join("state.db").try_exists()? {
        if read_binding(cfg)?.is_some() {
            return Err(Error::Changed);
        }
        return Ok(usage);
    }
    let conn = store::read_only(cfg)?;
    binding(cfg, &conn, false)?;
    let machine: Option<String> = conn
        .query_row("SELECT machine_id FROM sync_machine LIMIT 1", [], |r| {
            r.get(0)
        })
        .optional()?;
    let all = components(&conn)?;
    let names: BTreeSet<_> = local
        .keys()
        .chain(all.keys().map(|(name, _)| name))
        .collect();
    if names.len() > MAX_SKILLS {
        return Err(Error::TooLarge);
    }
    for ((name, origin), snapshot) in all {
        if Some(&origin) == machine.as_ref() {
            continue;
        }
        let row = &mut usage[&name];
        if row.is_null() {
            *row = json!({"uses":0,"ok":0,"fail":0});
        }
        row["_lore_local_counts"] = json!(local.get(&name).copied().unwrap_or_default());
        for (key, extra) in FIELDS.iter().zip(snapshot.counts().values()) {
            row[*key] = json!(row[*key]
                .as_u64()
                .unwrap_or(0)
                .checked_add(extra)
                .ok_or(Error::TooLarge)?);
        }
    }
    Ok(usage)
}

struct Plan {
    observations: BTreeMap<String, Local>,
    snapshots: Vec<Snapshot>,
    initialized: usize,
}
fn plan(conn: &Connection, machine: &str, raw: &Value) -> Result<Plan> {
    let counts = raw_counts(raw)?;
    let mut observations = locals(conn)?;
    let mut all = components(conn)?;
    let names: BTreeSet<_> = counts.keys().chain(observations.keys()).cloned().collect();
    if names.len() > MAX_SKILLS {
        return Err(Error::TooLarge);
    }
    let mut snapshots = Vec::new();
    let mut initialized = 0;
    for name in names {
        let now = counts.get(&name).copied().unwrap_or_default();
        let previous = observations.get(&name);
        let old = all.get(&(name.clone(), machine.to_owned()));
        if previous.is_none() && old.is_some() {
            // Losing just the baseline cannot relabel old totals as new work.
            return Err(Error::Changed);
        }
        if previous.is_some_and(|p| p.origin != machine) {
            return Err(Error::Changed);
        }
        let mut generation = previous.map_or(1, |p| p.generation);
        let mut next = old.map_or(Counts::default(), Snapshot::counts);
        if let Some(previous) = previous {
            if old.is_none_or(|s| s.generation != previous.generation) {
                return Err(Error::Changed);
            }
            if now.decreases(previous.last)
                || !counts.contains_key(&name) && previous.last != Counts::default()
            {
                generation = generation
                    .checked_add(1)
                    .filter(|n| *n <= MAX_GENERATION)
                    .ok_or(Error::TooLarge)?;
                // A reset establishes a fresh baseline. Unknown intervening
                // counts are local legacy, rather than fabricated observations.
                next = Counts::default();
            } else {
                next = next.advance(now, previous.last)?;
            }
        } else {
            initialized += 1;
        }
        let snapshot = Snapshot {
            v: 1,
            name: name.clone(),
            origin: machine.into(),
            generation,
            uses: next.uses,
            ok: next.ok,
            fail: next.fail,
        };
        snapshot.validate()?;
        capacity(&all, &name, machine)?;
        let changed = old.is_some_and(|old| old.generation != generation || old.counts() != next);
        all.insert((name.clone(), machine.into()), snapshot.clone());
        if changed {
            snapshots.push(snapshot);
        }
        observations.insert(
            name,
            Local {
                origin: machine.into(),
                generation,
                last: now,
            },
        );
    }
    Ok(Plan {
        observations,
        snapshots,
        initialized,
    })
}
fn commit_plan(cfg: &Config, conn: &Connection, plan: &Plan) -> Result<()> {
    schema(conn)?;
    for (name, local) in &plan.observations {
        conn.execute("INSERT INTO skill_usage_local(name,origin,generation,uses,ok,fail) VALUES(?,?,?,?,?,?)
            ON CONFLICT(name) DO UPDATE SET generation=excluded.generation,uses=excluded.uses,ok=excluded.ok,fail=excluded.fail",
            params![name,local.origin,sql_unsigned(local.generation)?,sql_unsigned(local.last.uses)?,sql_unsigned(local.last.ok)?,sql_unsigned(local.last.fail)?])?;
        if conn
            .query_row(
                "SELECT 1 FROM skill_usage_components WHERE name=? AND origin=?",
                params![name, local.origin],
                |_| Ok(()),
            )
            .optional()?
            .is_none()
        {
            put(
                conn,
                &Snapshot {
                    v: 1,
                    name: name.clone(),
                    origin: local.origin.clone(),
                    generation: local.generation,
                    uses: 0,
                    ok: 0,
                    fail: 0,
                },
            )?;
        }
    }
    for snapshot in &plan.snapshots {
        put(conn, snapshot)?;
        store::append_op(cfg, conn, "skill_usage", "snapshot", None, &json!(snapshot))?;
    }
    Ok(())
}

/// Caller holds the local usage file lock. Baselines commit before replacing
/// the file so a crash cannot turn legacy totals into fresh contributions.
/// The file commits first; a failed log transaction is recovered by reconcile.
pub(super) fn save_local(cfg: &Config, before: &Value, after: &Value) -> Result<()> {
    if !authoring(cfg) {
        return super::save_usage(cfg, after);
    }
    raw_counts(before)?;
    raw_counts(after)?;
    if serde_json::to_vec_pretty(after)
        .map_err(|_| Error::Unavailable)?
        .len()
        > crate::MAX_FRAME_BYTES
    {
        return Err(Error::TooLarge);
    }
    let mut baseline = before.clone();
    for name in after.as_object().ok_or(Error::InvalidRequest)?.keys() {
        if baseline.get(name).is_none() {
            baseline[name] = json!({"uses":0});
        }
    }
    let mut conn = store::connect(cfg)?;
    let machine = store::machine_id(&conn)?;
    binding(cfg, &conn, true)?;
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let initial = plan(&tx, &machine, &baseline)?;
    commit_plan(cfg, &tx, &initial)?;
    tx.commit()?;
    super::save_usage(cfg, after)?;
    let update = (|| {
        let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let next = plan(&tx, &machine, after)?;
        commit_plan(cfg, &tx, &next)?;
        tx.commit()?;
        Ok(())
    })();
    update.map_err(|_: Error| Error::MayHaveApplied)
}

/// Bounded reconcile/seed hook. First invocation baselines legacy rows without
/// exporting them. A dry run uses a read-only DB, or an empty in-memory DB;
/// it never creates identity, tables, lock files, or a store on disk.
pub fn reconcile(cfg: &Config, apply: bool) -> Result<Value> {
    if !enabled(cfg) {
        return Ok(json!({"enabled":false,"candidates":0,"seeded":0,"initialized":0}));
    }
    if !authoring(cfg) {
        return Err(Error::Untrusted);
    }
    let _lock = if apply {
        Some(files::Locks::acquire(
            &cfg.root,
            &[cfg.root.join("skill_usage.json")],
            cfg.timeout,
        )?)
    } else {
        None
    };
    let raw = super::load_local_usage(cfg)?;
    raw_counts(&raw)?;
    let mut conn = if apply {
        store::connect(cfg)?
    } else if cfg.root.join("state.db").try_exists()? {
        store::read_only(cfg)?
    } else {
        Connection::open_in_memory()?
    };
    if apply {
        store::machine_id(&conn)?;
        binding(cfg, &conn, true)?;
        let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let machine = store::machine_id(&tx)?;
        let next = plan(&tx, &machine, &raw)?;
        commit_plan(cfg, &tx, &next)?;
        tx.commit()?;
        Ok(
            json!({"enabled":true,"candidates":next.snapshots.len(),"seeded":next.snapshots.len(),"initialized":next.initialized}),
        )
    } else {
        binding(cfg, &conn, false)?;
        let machine = if has_table(&conn, "sync_machine")? {
            conn.query_row("SELECT machine_id FROM sync_machine LIMIT 1", [], |r| {
                r.get::<_, String>(0)
            })
            .optional()?
            .unwrap_or_else(|| "uninitialized".into())
        } else {
            "uninitialized".into()
        };
        let next = plan(&conn, &machine, &raw)?;
        Ok(
            json!({"enabled":true,"candidates":next.snapshots.len(),"seeded":0,"initialized":next.initialized}),
        )
    }
}
