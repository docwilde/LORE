//! Bounded native background hook scheduling. Stamps are locked before spawn;
//! background commands own their native supervisor independently of the hook.
use crate::{
    config::{self, Config},
    files, Error, Result,
};
use serde_json::Value;
use std::{
    path::{Path, PathBuf},
    process::{Command, Stdio},
};
fn now() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| d.as_secs())
}
fn due(cfg: &Config, path: &Path, interval: u64, start_clock: bool) -> Result<bool> {
    let _lock = files::Locks::acquire(&cfg.root, &[path.to_path_buf()], cfg.timeout)?;
    let previous = if path.try_exists()? {
        let bytes = files::read_regular(path, 1024)?;
        std::str::from_utf8(&bytes)
            .ok()
            .and_then(|s| s.trim().parse::<u64>().ok())
    } else {
        None
    };
    let current = now();
    if previous.is_some_and(|last| current.saturating_sub(last) < interval) {
        return Ok(false);
    }
    files::atomic_write(path, current.to_string().as_bytes())?;
    Ok(!start_clock || previous.is_some())
}
fn spawn(args: &[String], defer: bool) -> Result<()> {
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        let mut command = Command::new(std::env::current_exe()?);
        command
            .args(args)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .env_remove("LORE_NATIVE_SUPERVISOR_PID");
        if defer {
            command
                .env("LORE_DEFER_DREAM", "1")
                .env("LORE_NATIVE_BACKGROUND_REVIEW", "1");
        } else {
            command.env("LORE_NATIVE_BACKGROUND_PULL", "1");
        }
        unsafe {
            command.pre_exec(|| {
                if libc::setsid() < 0 {
                    return Err(std::io::Error::last_os_error());
                }
                #[cfg(target_os = "linux")]
                if libc::prctl(libc::PR_SET_PDEATHSIG, 0) != 0 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
        let mut child = command.spawn()?;
        // Hooks do not wait for work. Reap promptly if it exits before the hook;
        // otherwise its detached supervisor retains child ownership itself.
        std::thread::spawn(move || {
            let _ = child.wait();
        });
        Ok(())
    }
    #[cfg(not(unix))]
    {
        let _ = (args, defer);
        Err(Error::Unsupported)
    }
}
fn startup_pull_configured(enabled: bool, settings: &config::ResolvedSettings) -> bool {
    enabled
        && !settings.get("LORE_SYNC_PULL_AT_START").is_ok_and(|v| matches!(v.as_str(), "" | "0"))
        && ["LORE_SYNC_URL", "LORE_SYNC_PEER"].iter()
            .any(|key| settings.get(key).is_ok_and(|v| !v.trim().is_empty()))
}
pub fn pull_at_start(cfg: &Config, cwd: &str) -> Result<()> {
    if !startup_pull_configured(cfg.sync.enabled, &cfg.settings()?) {
        return Ok(());
    }
    let interval = cfg.var("LORE_SYNC_PULL_SECS")
        .ok()
        .and_then(|v| v.parse::<u64>().ok())
        .unwrap_or(120)
        .min(86400);
    if due(cfg, &cfg.root.join(".sync/pull"), interval, false)? {
        spawn(
            &["sync".into(), "pull".into(), "--cwd".into(), cwd.into()],
            false,
        )?;
    }
    Ok(())
}
pub fn review_at_prompt(
    cfg: &Config,
    input: &Value,
    cwd: &str,
    session: Option<&str>,
    engine: &str,
) -> Result<()> {
    if cfg.disabled("LORE_DISABLE_REVIEW") {
        return Ok(());
    }
    let Some(interval) = cfg.var("LORE_REVIEW_SECS")
        .ok()
        .and_then(|v| v.parse::<u64>().ok())
        .filter(|v| *v > 0 && *v <= 86400 * 30)
    else {
        return Ok(());
    };
    let Some(session) = session.filter(|s| config::valid_id(s)) else {
        return Ok(());
    };
    let Some(raw) = input["transcript_path"].as_str() else {
        return Ok(());
    };
    let path = PathBuf::from(raw);
    if path
        != cfg
            .projects
            .join(config::project_slug(Path::new(cwd)))
            .join(format!("{session}.jsonl"))
        && !path.starts_with(&cfg.codex_sessions)
    {
        return Err(Error::UnsafePath);
    }
    // First prompt starts the clock and never schedules paid work.
    if due(
        cfg,
        &cfg.root.join(".midreview").join(session),
        interval,
        true,
    )? {
        spawn(
            &[
                "review".into(),
                "--transcript".into(),
                raw.into(),
                "--cwd".into(),
                cwd.into(),
                "--session-id".into(),
                session.into(),
                "--engine".into(),
                engine.into(),
                "--foreground".into(),
                "--incremental".into(),
            ],
            true,
        )?;
    }
    Ok(())
}

#[cfg(test)]
mod persisted_startup_tests {
    use super::*;
    #[test]
    fn saved_destination_triggers_startup_without_network() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("settings.json");
        files::atomic_write(&path, br#"{"env":{"LORE_SYNC_URL":"https://example.invalid"}}"#).unwrap();
        let settings = config::ResolvedSettings::from_file(&path).unwrap();
        assert!(startup_pull_configured(true, &settings));
        assert!(!startup_pull_configured(false, &settings));
        files::atomic_write(&path, br#"{"env":{"LORE_SYNC_PEER":"fixture.invalid","LORE_SYNC_PULL_AT_START":"0"}}"#).unwrap();
        let disabled = config::ResolvedSettings::from_file(&path).unwrap();
        assert!(!startup_pull_configured(true, &disabled));
        assert!(!startup_pull_configured(true, &config::ResolvedSettings::default()));
    }
}
