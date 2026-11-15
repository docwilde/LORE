//! Focused opt-in seed hook, kept separate from the portable-content planner.
use crate::{config::Config, Result};
use serde_json::Value;

/// Parent sync-seed integration: include this class report and add its
/// `candidates` / `seeded` fields to the command's aggregate counts. This hook
/// acquires its own usage lock and SQLite transaction; call outside another
/// database transaction. Dry runs never write to the usage store.
pub fn skill_usage(cfg: &Config, apply: bool) -> Result<Value> {
    super::usage_sync::reconcile(cfg, apply)
}
