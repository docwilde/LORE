//! Native carrier status and local identity. Remote transport configuration
//! controls chip visibility; a read never fabricates an empty healthy store.
use crate::{
    config::Config,
    gate::Authority,
    memory::{self, Scope},
    store, Error, Result,
};
use rusqlite::{params, OptionalExtension, TransactionBehavior};
use serde_json::{json, Value};

fn configured(cfg: &Config) -> bool {
    cfg.sync.enabled
        && ["LORE_SYNC_URL", "LORE_SYNC_PEER"]
            .iter()
            .any(|name| std::env::var(name).is_ok_and(|s| !s.trim().is_empty()))
}
pub fn machine(cfg: &Config, create: bool) -> Result<Value> {
    if create {
        if !configured(cfg) {
            return Ok(Value::Null);
        }
        let mut conn = store::connect(cfg)?;
        let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let id = store::machine_id(&tx)?;
        tx.commit()?;
        return Ok(json!(id));
    }
    let conn = match store::read_only(cfg) {
        Ok(conn) => conn,
        Err(_) => return Ok(Value::Null),
    };
    Ok(json!(conn
        .query_row("SELECT machine_id FROM sync_machine LIMIT 1", [], |r| {
            r.get::<_, String>(0)
        })
        .optional()?))
}
pub fn state(cfg: &Config) -> Result<Value> {
    if !configured(cfg) {
        return Ok(Value::Null);
    }
    match read_state(cfg) {
        Ok(state) => Ok(state),
        Err(_) => Ok(Value::Null),
    }
}
fn read_state(cfg: &Config) -> Result<Value> {
    let (hub, active) = crate::sync_network::configured_cursor_keys()?;
    let conn = store::read_only(cfg)?;
    let mine = conn
        .query_row("SELECT machine_id FROM sync_machine LIMIT 1", [], |r| {
            r.get::<_, String>(0)
        })
        .optional()?;
    let unpushed = if let (Some(mine), Some(hub)) = (mine, hub) {
        conn.query_row("SELECT count(*) FROM sync_ops WHERE machine_id=? AND seq>(SELECT coalesce((SELECT pushed_seq FROM sync_peers WHERE peer=?),0))",params![mine,hub],|r|crate::beliefs::sql_count(r,0))?
    } else {
        0
    };
    let unverified = conn.query_row("SELECT count(*) FROM sync_ops WHERE applied=2", [], |r| {
        crate::beliefs::sql_count(r, 0)
    })?;
    let mut latest = None;
    let mut pull = conn.prepare("SELECT last_pull FROM sync_peers WHERE peer=?")?;
    for key in active {
        let stamp = pull
            .query_row([key], |r| r.get::<_, Option<String>>(0))
            .optional()?
            .flatten();
        if let Some(when) = stamp.and_then(|stamp| {
            time::OffsetDateTime::parse(&stamp, &time::format_description::well_known::Rfc3339).ok()
        }) {
            latest = Some(latest.map_or(when, |previous: time::OffsetDateTime| previous.max(when)));
        }
    }
    let age = latest.map(|when| {
        (time::OffsetDateTime::now_utc() - when)
            .as_seconds_f64()
            .max(0.)
    });
    let mut conflicts = 0u64;
    let mut stmt = conn.prepare(
        "SELECT kind,bucket,a_text,b_text FROM sync_conflicts ORDER BY created,bucket LIMIT 4097",
    )?;
    for (index, row) in stmt
        .query_map([], |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, String>(2)?,
                r.get::<_, String>(3)?,
            ))
        })?
        .enumerate()
    {
        if index >= 4096 {
            return Err(Error::TooLarge);
        }
        let (kind, bucket, a, b) = row?;
        let entries = match kind.as_str() {
            "memory" => {
                let (scope, key) = if bucket == "user" {
                    (Scope::User, "user")
                } else {
                    (
                        Scope::Project,
                        bucket
                            .strip_prefix("project:")
                            .ok_or(Error::InvalidRequest)?,
                    )
                };
                Some(memory::read_entries(&scope.path(cfg, key)?)?)
            }
            "filemap" => Some(memory::read_entries(&crate::filemap::path(cfg, &bucket)?)?),
            _ => None,
        };
        if entries.is_none_or(|entries| {
            entries.iter().any(|e| e.to_lowercase() == a.to_lowercase())
                && entries.iter().any(|e| e.to_lowercase() == b.to_lowercase())
        }) {
            conflicts += 1;
        }
    }
    Ok(
        json!({"last_pull_age_s":age,"unpushed":unpushed,"conflicts":conflicts,"unverified":unverified}),
    )
}
pub fn record(cfg: &Config, req: &Value, authority: &Authority) -> Result<Value> {
    if !authority.may_write() {
        return Err(Error::Untrusted);
    }
    let class = crate::beliefs::text(req, "class", 32)?;
    if !matches!(class, "tabsets" | "worktrees")
        || !configured(cfg)
        || !cfg.sync.classes.contains(class)
    {
        return Err(Error::Untrusted);
    }
    let op = crate::beliefs::text(req, "action", 32)?;
    let slug = req["slug"]
        .as_str()
        .filter(|s| crate::config::valid_slug(s))
        .ok_or(Error::InvalidRequest)?;
    crate::gate::append_file_op(cfg, class, op, Some(slug), &req["payload"])?;
    Ok(json!({"recorded":true}))
}
pub fn project(cfg: &Config, req: &Value) -> Result<Value> {
    let conn = match store::read_only(cfg) {
        Ok(conn) => conn,
        Err(_) => return Ok(Value::Null),
    };
    let slug = crate::config::project_slug(crate::gate::cwd(req)?);
    Ok(json!(conn
        .query_row(
            "SELECT project_key FROM sync_projects WHERE slug=?",
            params![slug],
            |r| r.get::<_, String>(0)
        )
        .optional()?))
}
