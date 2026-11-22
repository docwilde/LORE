//! Provider-reported tool observations and explicit links to beliefs or recipes.
use crate::{config, gate, scrub, skills, store, Error, Result};
use rusqlite::{params, OptionalExtension, TransactionBehavior};
use serde_json::{json, Value};
use std::path::Path;

const MAX_BATCH: usize = 128;
const SCHEMA: &str = "
    CREATE TABLE IF NOT EXISTS tool_observations (
        id INTEGER PRIMARY KEY,
        project TEXT NOT NULL,
        source TEXT NOT NULL,
        engine TEXT NOT NULL,
        session_id TEXT NOT NULL,
        call_id TEXT NOT NULL,
        tool TEXT NOT NULL,
        outcome TEXT NOT NULL CHECK (outcome IN ('success', 'failure')),
        recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (project, source, engine, session_id, call_id)
    );
    CREATE INDEX IF NOT EXISTS tool_observations_project_id ON tool_observations(project, id);
    CREATE TABLE IF NOT EXISTS tool_observation_links (
        observation_id INTEGER NOT NULL REFERENCES tool_observations(id),
        target_kind TEXT NOT NULL CHECK (target_kind IN ('belief', 'recipe')),
        target_id TEXT NOT NULL,
        PRIMARY KEY (observation_id, target_kind, target_id)
    );";

pub(crate) fn enabled(cfg: &config::Config) -> bool {
    cfg.var("LORE_TOOL_EVIDENCE").as_deref() != Ok("0")
        && !cfg.var("LORE_SKIP").is_ok_and(|value| !value.is_empty())
}

fn token(value: &Value, max: usize) -> Result<&str> {
    value
        .as_str()
        .filter(|s| {
            !s.is_empty()
                && s.len() <= max
                && s.bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"_-.:/".contains(&b))
        })
        .ok_or(Error::InvalidRequest)
}

fn project(cwd: &Path) -> Result<String> {
    let slug = config::project_slug(cwd);
    if slug.len() > 4096 || scrub::scrub(&slug)? != slug {
        return Err(Error::InvalidRequest);
    }
    Ok(slug)
}

pub(crate) fn record(cfg: &config::Config, req: &Value, source: &str) -> Result<Value> {
    if !enabled(cfg) {
        return Ok(json!({"inserted": 0, "enabled": false}));
    }
    let cwd = gate::cwd(req)?;
    let engine = req["engine"]
        .as_str()
        .filter(|s| matches!(*s, "claude" | "codex" | "antigravity" | "deepseek" | "glm"))
        .ok_or(Error::InvalidRequest)?;
    let session = req["session_id"]
        .as_str()
        .filter(|s| config::valid_id(s))
        .ok_or(Error::InvalidRequest)?;
    let input = req["observations"]
        .as_array()
        .filter(|batch| !batch.is_empty() && batch.len() <= MAX_BATCH)
        .ok_or(Error::InvalidRequest)?;
    let mut observations = Vec::with_capacity(input.len());
    for item in input {
        let id = token(&item["call_id"], 256)?;
        let tool = token(&item["tool"], 128)?;
        let outcome = item["outcome"]
            .as_str()
            .filter(|s| matches!(*s, "success" | "failure"))
            .ok_or(Error::InvalidRequest)?;
        if scrub::scrub(id)? != id || scrub::scrub(tool)? != tool {
            return Err(Error::InvalidRequest);
        }
        observations.push((id, tool, outcome));
    }
    if scrub::scrub(session)? != session {
        return Err(Error::InvalidRequest);
    }
    let project = project(cwd)?;
    let mut db = store::connect(cfg)?;
    db.execute_batch(SCHEMA)?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let mut inserted = 0;
    for (call_id, tool, outcome) in observations {
        let previous = tx
            .query_row(
                "SELECT tool, outcome FROM tool_observations
             WHERE project=? AND source=? AND engine=? AND session_id=? AND call_id=?",
                params![project, source, engine, session, call_id],
                |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)),
            )
            .optional()?;
        if let Some((old_tool, old_outcome)) = previous {
            if old_tool != tool || old_outcome != outcome {
                return Err(Error::Changed);
            }
            continue;
        }
        tx.execute(
            "INSERT INTO tool_observations(project, source, engine, session_id, call_id, tool, outcome)
             VALUES (?, ?, ?, ?, ?, ?, ?)",
            params![project, source, engine, session, call_id, tool, outcome],
        )?;
        inserted += 1;
    }
    tx.commit()?;
    Ok(json!({"inserted": inserted, "enabled": true}))
}

fn codex_outcome(input: &Value) -> Option<&'static str> {
    let response = &input["tool_response"];
    let code = response["exit_code"].as_i64();
    if let Some(code) = code {
        return Some(if code == 0 { "success" } else { "failure" });
    }
    if input["tool_name"]
        .as_str()
        .is_some_and(|name| name.starts_with("mcp__"))
        && response["content"].is_array()
    {
        return match response.get("isError") {
            None | Some(Value::Bool(false)) => Some("success"),
            Some(Value::Bool(true)) => Some("failure"),
            _ => None,
        };
    }
    None
}

pub(crate) fn hook(cfg: &config::Config, engine: &str, event: &str, input: &Value) -> Result<()> {
    if !matches!(
        (engine, event),
        ("claude", "PostToolUse" | "PostToolUseFailure") | ("codex" | "antigravity", "PostToolUse")
    ) {
        return Err(Error::InvalidRequest);
    }
    if engine != "antigravity" && input["hook_event_name"] != event {
        return Err(Error::InvalidRequest);
    }
    let (cwd, session, call_id, tool, outcome) = if engine == "antigravity" {
        let paths = input["workspacePaths"]
            .as_array()
            .filter(|rows| rows.len() == 1)
            .ok_or(Error::InvalidRequest)?;
        let step = input["stepIdx"].as_u64().ok_or(Error::InvalidRequest)?;
        let outcome = match input.get("error") {
            None => "success",
            Some(Value::String(error)) => {
                if error.is_empty() {
                    "success"
                } else {
                    "failure"
                }
            }
            _ => return Err(Error::InvalidRequest),
        };
        (
            &paths[0],
            &input["conversationId"],
            format!("step-{step}"),
            &input["toolCall"]["name"],
            outcome,
        )
    } else {
        let outcome = if engine == "codex" {
            let Some(outcome) = codex_outcome(input) else {
                return Ok(());
            };
            outcome
        } else if event == "PostToolUseFailure" {
            "failure"
        } else {
            "success"
        };
        let id = token(&input["tool_use_id"], 192)?.to_owned();
        (
            &input["cwd"],
            &input["session_id"],
            id,
            &input["tool_name"],
            outcome,
        )
    };
    record(
        cfg,
        &json!({"cwd":cwd,"engine":engine,"session_id":session,
        "observations":[{"call_id":call_id,"tool":tool,"outcome":outcome}]}),
        "hook",
    )?;
    Ok(())
}

fn positive(value: &Value) -> Result<i64> {
    value
        .as_i64()
        .or_else(|| value.as_str()?.parse().ok())
        .filter(|n| *n > 0)
        .ok_or(Error::InvalidRequest)
}

fn target(cfg: &config::Config, req: &Value) -> Result<Option<(&'static str, String)>> {
    match (req.get("belief"), req.get("recipe")) {
        (Some(id), None) => Ok(Some(("belief", positive(id)?.to_string()))),
        (None, Some(recipe)) => {
            let name = recipe
                .as_str()
                .filter(|s| config::valid_id(s))
                .ok_or(Error::InvalidRequest)?;
            if !skills::learned(cfg)?.contains_key(name) {
                return Err(Error::NoMatch);
            }
            let bytes = crate::files::read_regular(
                &cfg.skills.join(name).join("SKILL.md"),
                crate::MAX_FRAME_BYTES,
            )?;
            Ok(Some((
                "recipe",
                format!("{name}@{}", crate::digest(&bytes)),
            )))
        }
        (None, None) => Ok(None),
        _ => Err(Error::InvalidRequest),
    }
}

pub(crate) fn list(cfg: &config::Config, req: &Value) -> Result<Value> {
    let slug = project(gate::cwd(req)?)?;
    let target = target(cfg, req)?;
    let limit = req.get("limit").map(positive).transpose()?.unwrap_or(30);
    if limit > 100 {
        return Err(Error::InvalidRequest);
    }
    let before = req.get("before").map(positive).transpose()?;
    let db = store::connect(cfg)?;
    db.execute_batch(SCHEMA)?;
    let mut query = db.prepare(
        "SELECT id, source, engine, session_id, call_id, tool, outcome, recorded_at
         FROM tool_observations
         WHERE project=?1 AND (?2 IS NULL OR id < ?2)
           AND (?4 IS NULL OR EXISTS (
             SELECT 1 FROM tool_observation_links
             WHERE observation_id=tool_observations.id AND target_kind=?4 AND target_id=?5))
         ORDER BY id DESC LIMIT ?3",
    )?;
    let rows = query.query_map(
        params![
            slug,
            before,
            limit,
            target.as_ref().map(|(kind, _)| *kind),
            target.as_ref().map(|(_, id)| id)
        ],
        |row| {
            Ok(
                json!({"id":row.get::<_, i64>(0)?, "source":row.get::<_, String>(1)?,
            "engine":row.get::<_, String>(2)?, "session_id":row.get::<_, String>(3)?,
            "call_id":row.get::<_, String>(4)?, "tool":row.get::<_, String>(5)?,
            "outcome":row.get::<_, String>(6)?, "recorded_at":row.get::<_, String>(7)?}),
            )
        },
    )?;
    Ok(json!(rows.collect::<std::result::Result<Vec<_>, _>>()?))
}

pub(crate) fn link(
    cfg: &config::Config,
    req: &Value,
    authority: &gate::Authority,
) -> Result<Value> {
    authority.require_review()?;
    let slug = project(gate::cwd(req)?)?;
    let id = positive(&req["observation_id"])?;
    let (kind, target) = target(cfg, req)?.ok_or(Error::InvalidRequest)?;
    let mut db = store::connect(cfg)?;
    db.execute_batch(SCHEMA)?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let visible: bool = tx.query_row(
        "SELECT EXISTS(SELECT 1 FROM tool_observations WHERE project=? AND id=?)",
        params![slug, id],
        |row| row.get(0),
    )?;
    if !visible {
        return Err(Error::NoMatch);
    }
    if kind == "belief" {
        let visible: bool = tx.query_row(
            "SELECT EXISTS(SELECT 1 FROM beliefs WHERE id=? AND subject IN ('user', 'user-model', ?))",
            params![target, format!("project:{slug}")], |row| row.get(0))?;
        if !visible {
            return Err(Error::NoMatch);
        }
    }
    tx.execute(
        "INSERT OR IGNORE INTO tool_observation_links VALUES (?, ?, ?)",
        params![id, kind, target],
    )?;
    tx.commit()?;
    Ok(json!({"observation_id":id,"target_kind":kind,"target_id":target}))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn owner_links_stay_scoped_and_preserve_belief_and_recipe_outcomes() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path();
        let cfg = config::Config {
            root: root.join("lore"),
            skills: root.join("skills"),
            projects: root.join("projects"),
            codex_sessions: root.join("codex"),
            user_cap: 9000,
            project_cap: 8800,
            machine_cap: 3000,
            filemap_cap: 4000,
            timeout: std::time::Duration::from_secs(1),
            sync: config::SyncConfig {
                enabled: false,
                classes: Default::default(),
                key: None,
            },
        };
        let recipe = cfg.skills.join("fixture-recipe");
        std::fs::create_dir_all(&recipe).unwrap();
        std::fs::write(
            recipe.join("SKILL.md"),
            "---\nname: fixture-recipe\ndescription: A fixture\n---\nlore-learned",
        )
        .unwrap();
        let slug = project(root).unwrap();
        let db = store::connect(&cfg).unwrap();
        db.execute_batch(SCHEMA).unwrap();
        db.execute(
            "INSERT INTO beliefs(id,subject,claim,confidence) VALUES (1,?,'fixture belief',0.5)",
            [format!("project:{slug}")],
        )
        .unwrap();
        db.execute("INSERT INTO beliefs(id,subject,claim,confidence) VALUES (2,'project:foreign','outside',0.5)",
            []).unwrap();
        db.execute(
            "INSERT INTO tool_observations(project,source,engine,session_id,call_id,tool,outcome)
            VALUES (?,'hook','claude','fixture','test-1','Bash','failure')",
            [slug],
        )
        .unwrap();

        let human = gate::Authority::HumanReview {
            agent: "fixture".into(),
            engine: "human".into(),
        };
        for target in [json!({"belief":1}), json!({"recipe":"fixture-recipe"})] {
            let mut request = target.clone();
            request["cwd"] = json!(root);
            assert_eq!(list(&cfg, &request).unwrap(), json!([]));
            request["observation_id"] = json!(1);
            for _ in 0..2 {
                link(&cfg, &request, &human).unwrap();
            }
            let list = list(&cfg, &request).unwrap();
            assert_eq!(list.as_array().unwrap().len(), 1);
            assert_eq!(list[0]["outcome"], "failure");
        }
        assert_eq!(
            link(
                &cfg,
                &json!({"cwd":root,"observation_id":1,"belief":2}),
                &human
            ),
            Err(Error::NoMatch)
        );
        assert_eq!(
            db.query_row("SELECT count(*) FROM belief_evidence", [], |row| row
                .get::<_, i64>(0))
                .unwrap(),
            0
        );
        assert_eq!(
            db.query_row("SELECT confidence FROM beliefs WHERE id=1", [], |row| row
                .get::<_, f64>(
                0
            ))
            .unwrap(),
            0.5
        );
        assert!(!cfg.root.join("skill_usage.json").exists());
        std::fs::write(
            recipe.join("SKILL.md"),
            "---
name: fixture-recipe
---
lore-learned revised",
        )
        .unwrap();
        assert_eq!(
            list(&cfg, &json!({"cwd":root,"recipe":"fixture-recipe"})).unwrap(),
            json!([])
        );
    }
}
