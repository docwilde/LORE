//! Opt-in project file map; no automatic scan or inferred facts.
use crate::{
    config::{project_identity_root, project_slug, valid_slug, Config},
    files,
    gate::{self, Authority},
    memory, Error, Result,
};
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
pub const SEP: &str = " — ";
pub fn path(cfg: &Config, slug: &str) -> Result<PathBuf> {
    if !valid_slug(slug) {
        return Err(Error::UnsafePath);
    }
    Ok(cfg.root.join("filemap").join(format!("{slug}.md")))
}
pub fn normalize_map_path(value: &str, root: Option<&Path>) -> String {
    let path = Path::new(value);
    if path.is_absolute() {
        if let Some(root) = root {
            if let Ok(relative) = path.strip_prefix(root) {
                return if relative.as_os_str().is_empty() {
                    ".".into()
                } else {
                    relative.to_string_lossy().into_owned()
                };
            }
        }
    }
    value.into()
}
pub fn entries_for(cfg: &Config, slug: &str) -> Result<Vec<(String, String)>> {
    Ok(memory::read_entries(&path(cfg, slug)?)?
        .iter()
        .map(|entry| {
            let (path, purpose) = entry.split_once(SEP).unwrap_or((entry.as_str(), ""));
            (path.trim().into(), purpose.trim().into())
        })
        .collect())
}
pub fn show(cfg: &Config, req: &Value) -> Result<Value> {
    let slug = project_slug(gate::cwd(req)?);
    let rows = entries_for(cfg, &slug)?;
    let rows = rows
        .into_iter()
        .map(|(path, purpose)| {
            Ok(json!({"path":crate::scrub::scrub(&path)?,"purpose":crate::scrub::scrub(&purpose)?}))
        })
        .collect::<Result<Vec<_>>>()?;
    Ok(json!({"key":slug,"entries":rows,"cap_chars":cfg.filemap_cap}))
}
pub fn mutate(
    cfg: &Config,
    slug: &str,
    action: &str,
    needle: &str,
    map_path: &str,
    purpose: &str,
    root: Option<&Path>,
    via: &str,
    authority: &Authority,
) -> Result<()> {
    let file = path(cfg, slug)?;
    let _lock = files::Locks::acquire(&cfg.root, &[file], cfg.timeout)?;
    mutate_locked(
        cfg, slug, action, needle, map_path, purpose, root, via, authority,
    )
}
pub(crate) fn mutate_locked(
    cfg: &Config,
    slug: &str,
    action: &str,
    needle: &str,
    map_path: &str,
    purpose: &str,
    root: Option<&Path>,
    via: &str,
    authority: &Authority,
) -> Result<()> {
    if !authority.may_write() {
        return Err(Error::Untrusted);
    }
    let file = path(cfg, slug)?;
    let mut entries = memory::read_entries(&file)?;
    let map_path = normalize_map_path(&gate::one_line(&crate::scrub::scrub(map_path)?), root);
    let purpose = gate::one_line(&crate::scrub::scrub(purpose)?);
    let text = format!("{map_path}{SEP}{purpose}");
    let mut op = action;
    let mut old = None;
    match action {
        "add" => {
            if map_path.is_empty() || purpose.is_empty() {
                return Err(Error::InvalidRequest);
            }
            if let Some(index) = entries.iter().position(|entry| {
                entry
                    .split_once(SEP)
                    .map_or(entry.as_str(), |(path, _)| path)
                    .trim()
                    .to_lowercase()
                    == map_path.to_lowercase()
            }) {
                if entries[index].to_lowercase() == text.to_lowercase() {
                    return Ok(());
                }
                old = Some(entries[index].clone());
                entries[index] = text.clone();
                op = "replace";
            } else {
                entries.push(text.clone());
            }
        }
        "replace" | "remove" => {
            let hits = memory::match_entries(&entries, needle);
            if hits.len() != 1 {
                return Err(Error::Changed);
            }
            let index = hits[0];
            old = Some(entries[index].clone());
            if action == "remove" {
                entries.remove(index);
            } else {
                entries[index] = text.clone();
            }
        }
        _ => return Err(Error::InvalidRequest),
    }
    memory::write_entries(&file, &entries, cfg.filemap_cap)?;
    if let Some(old) = &old {
        gate::forget(cfg, "filemap", slug, old)?;
    }
    if action != "remove" {
        gate::record(cfg, "filemap", slug, &text, via, None, authority, None)?;
    }
    let payload = match op {
        "add" => json!({"text":text,"via":via,"writer":authority.writer()}),
        "replace" => {
            json!({"old_key":gate::entry_key("filemap",slug,old.as_deref().unwrap()),"text":text,"via":via,"writer":authority.writer()})
        }
        _ => json!({"key":gate::entry_key("filemap",slug,old.as_deref().unwrap())}),
    };
    gate::append_file_op(cfg, "filemap", op, Some(slug), &payload)
        .map_err(|_| Error::MayHaveApplied)
}
pub fn action(cfg: &Config, req: &Value, authority: &Authority) -> Result<Value> {
    let cwd = gate::cwd(req)?;
    let slug = project_slug(cwd);
    let action = req["action"]
        .as_str()
        .filter(|action| matches!(*action, "add" | "replace" | "remove"))
        .ok_or(Error::InvalidRequest)?;
    let needle = req
        .get("match")
        .map_or(Some(""), Value::as_str)
        .ok_or(Error::InvalidRequest)?;
    let path = req
        .get("path")
        .map_or(Some(""), Value::as_str)
        .ok_or(Error::InvalidRequest)?;
    let purpose = req
        .get("purpose")
        .map_or(Some(""), Value::as_str)
        .ok_or(Error::InvalidRequest)?;
    if !authority.may_write() {
        let id = gate::stage(
            cfg,
            &json!({"kind":"filemap","action":action,"project":slug,"path":path,"purpose":purpose,"match":needle}),
            authority,
        )?;
        return Ok(json!({"status":"staged","pid":id}));
    }
    let file = self::path(cfg, &slug)?;
    let _lock = files::Locks::acquire(&cfg.root, &[file.clone()], cfg.timeout)?;
    memory::observe_file_write(&[file], || {
        mutate_locked(
            cfg,
            &slug,
            action,
            needle,
            path,
            purpose,
            Some(&project_identity_root(cwd)),
            "direct",
            authority,
        )
    })
    .map(memory::report_file_write)
}

#[cfg(test)]
mod tests {
    #[test]
    fn landed_filemap_with_failed_enabled_log_reports_partial_effect() {
        let temp = tempfile::tempdir().unwrap();
        let mut cfg = Config::for_root(temp.path().join("lore"));
        cfg.sync.enabled = true;
        cfg.sync.classes = ["filemap".into()].into_iter().collect();
        files::private_dir(&cfg.root).unwrap();
        std::fs::write(cfg.root.join("state.db"), b"owned corrupt fixture").unwrap();
        let auth = Authority::HumanReview {
            agent: "fixture".into(),
            engine: "codex".into(),
        };
        let result = action(
            &cfg,
            &json!({"cwd":temp.path(),"action":"add","path":"src.rs","purpose":"owned fixture"}),
            &auth,
        )
        .unwrap();
        assert_eq!(result["status"], "refused");
        assert_eq!(result["applied"], true);
        assert_eq!(result["error"], "may_have_applied");
        assert_eq!(
            entries_for(&cfg, &project_slug(temp.path())).unwrap(),
            [("src.rs".into(), "owned fixture".into())]
        );
    }

    use super::*;
    #[test]
    fn keyed_update_and_root_relative_paths_are_canonical() {
        let temp = tempfile::tempdir().unwrap();
        let cfg = Config::for_root(temp.path().join("lore"));
        let authority = Authority::Interactive {
            agent: "fixture".into(),
            engine: "codex".into(),
        };
        let root = temp.path().join("repo");
        let file = root.join("data.json");
        mutate(
            &cfg,
            "project",
            "add",
            "",
            file.to_str().unwrap(),
            "first",
            Some(&root),
            "direct",
            &authority,
        )
        .unwrap();
        mutate(
            &cfg,
            "project",
            "add",
            "",
            "data.json",
            "updated",
            Some(&root),
            "direct",
            &authority,
        )
        .unwrap();
        assert_eq!(
            entries_for(&cfg, "project").unwrap(),
            [("data.json".into(), "updated".into())]
        );
        assert_eq!(
            normalize_map_path("host:~/data", Some(&root)),
            "host:~/data"
        );
        assert!(path(&cfg, "../outside").is_err());
    }
}
