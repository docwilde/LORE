use std::{env, path::{Path, PathBuf}, time::Duration};
use crate::{Error, Result};

#[derive(Clone, Debug)]
pub struct Config {
    pub root: PathBuf,
    pub skills: PathBuf,
    pub projects: PathBuf,
    pub codex_sessions: PathBuf,
    pub user_cap: usize,
    pub project_cap: usize,
    pub machine_cap: usize,
    pub filemap_cap: usize,
    pub timeout: Duration,
}
impl Config {
    pub fn from_env(timeout: Duration) -> Result<Self> {
        let home = env::var_os("HOME").map(PathBuf::from).ok_or(Error::Unavailable)?;
        let default_root = home.join(".claude/lore");
        let root = env::var_os("LORE_ROOT").map(PathBuf::from).unwrap_or_else(|| default_root.clone());
        if !root.is_absolute() || timeout.is_zero() { return Err(Error::InvalidRequest); }
        let skills = env::var_os("LORE_SKILLS_DIR").map(PathBuf::from)
            .unwrap_or_else(|| if root == default_root { home.join(".claude/skills") } else { root.join("skills") });
        let projects = env::var_os("LORE_PROJECTS_DIR").map(PathBuf::from).unwrap_or_else(|| home.join(".claude/projects"));
        let codex_home = env::var_os("CODEX_HOME").map(PathBuf::from).unwrap_or_else(|| home.join(".codex"));
        let codex_sessions = env::var_os("LORE_CODEX_SESSIONS_DIR").map(PathBuf::from).unwrap_or_else(|| codex_home.join("sessions"));
        Ok(Self { root, skills, projects, codex_sessions,
            user_cap: cap("LORE_USER_CAP", 9000)?, project_cap: cap("LORE_MEMORY_CAP", 8800)?,
            machine_cap: cap("LORE_MACHINE_CAP", 4400)?, filemap_cap: cap("LORE_FILEMAP_CAP", 4400)?, timeout })
    }
    pub fn for_root(root: PathBuf) -> Self {
        Self { skills: root.join("skills"), projects: root.join("session-projects"),
            codex_sessions: root.join("codex-sessions"), root, user_cap:9000, project_cap:8800,
            machine_cap:4400, filemap_cap:4400, timeout:Duration::from_secs(3) }
    }
}
fn cap(name: &str, default: usize) -> Result<usize> {
    match env::var(name) {
        Ok(raw) => raw.trim().parse::<usize>().ok().filter(|n| *n > 0 && *n <= 1024*1024).ok_or(Error::InvalidRequest),
        Err(env::VarError::NotPresent) => Ok(default),
        Err(_) => Err(Error::InvalidRequest),
    }
}
pub fn valid_slug(s: &str) -> bool {
    !s.is_empty() && s.len() <= 4096 && !s.contains("..") && !s.chars().any(|c| matches!(c,'/'|'\\'|'\0'))
}
pub fn valid_skill_name(s: &str) -> bool {
    !s.is_empty() && s.len() <= 128 && s.split('-').all(|part| !part.is_empty() && part.bytes().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit()))
}
pub fn valid_id(s: &str) -> bool {
    !s.is_empty() && s.len() <= 128 && s.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-' || c == b'_')
}

/// Resolve standard Git layouts without launching a subprocess or hooks.
/// Linked worktrees share their main checkout's memory identity.
pub fn project_identity_root(cwd: &Path) -> PathBuf {
    for candidate in cwd.ancestors() {
        let git = candidate.join(".git");
        if git.is_dir() { return candidate.to_path_buf(); }
        if let Ok(bytes) = crate::files::read_regular(&git,4096) {
            if let Ok(text) = std::str::from_utf8(&bytes) {
                if let Some(path) = text.trim().strip_prefix("gitdir: ") {
                    let target = candidate.join(path);
                    if let Ok(target) = target.canonicalize() {
                        if target.parent().and_then(Path::file_name).is_some_and(|p| p == "worktrees") {
                            if let Some(root) = target.parent().and_then(Path::parent).and_then(Path::parent) {
                                if root.join(".git").is_dir() { return root.to_path_buf(); }
                            }
                        }
                        return candidate.to_path_buf();
                    }
                }
            }
        }
    }
    // Recorded sessions can outlive deleted worktrees. Accept the inferred
    // parent only when it is still an actual repository.
    for candidate in cwd.ancestors() {
        if candidate.file_name().and_then(|p|p.to_str()).is_some_and(|s| s.ends_with("worktree") || s.ends_with("worktrees")) {
            if let Some(root) = candidate.parent().filter(|r| r.join(".git").is_dir()) { return root.to_path_buf(); }
        }
    }
    cwd.to_path_buf()
}
pub fn project_slug(cwd: &Path) -> String {
    project_identity_root(cwd).to_string_lossy().chars().map(|c|if c.is_ascii_alphanumeric(){c}else{'-'}).collect()
}
pub fn project_key(cwd: &Path) -> String {
    let root=project_identity_root(cwd);
    let config=root.join(".git/config");
    let Ok(raw)=crate::files::read_regular(&config,64*1024) else {return project_slug(cwd)};
    let Ok(text)=std::str::from_utf8(&raw) else {return project_slug(cwd)};
    let mut origin=false;
    for line in text.lines().map(str::trim) {
        if line.starts_with('[') {origin=line=="[remote \"origin\"]";continue;}
        if origin {if let Some((key,value))=line.split_once('=') {if key.trim()=="url" {return normalize_origin(value.trim());}}}
    }
    project_slug(cwd)
}
pub fn normalize_origin(raw:&str)->String {
    let raw=raw.trim();
    let raw=raw.split_once("://").map_or(raw,|(_,rest)|rest);
    let mut value=if let Some((_,host_path))=raw.split_once('@') {
        host_path.replacen(':',"/",1)
    } else {raw.to_owned()};
    if value.ends_with(".git") {value.truncate(value.len()-4);}
    value.to_lowercase()
}
