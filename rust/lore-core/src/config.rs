use crate::{Error, Result};
use std::{
    env,
    path::{Path, PathBuf},
    time::Duration,
};

/// Shared, bounded settings lookup. Values and credentials are never Debug output.
#[derive(Clone, Default)]
pub struct ResolvedSettings {
    saved: std::collections::HashMap<String, String>,
}
impl ResolvedSettings {
    pub fn from_env() -> Result<Self> {
        Self::from_env_with_root(None)
    }
    pub fn from_env_with_root(effective_root: Option<&Path>) -> Result<Self> {
        let home = env::var_os("HOME").map(PathBuf::from).ok_or(Error::Unavailable)?;
        let directory = env::var_os("CLAUDE_CONFIG_DIR").map(PathBuf::from)
            .unwrap_or_else(|| home.join(".claude"));
        let root = effective_root.map(PathBuf::from).or_else(|| env::var_os("LORE_ROOT").filter(|v| !v.is_empty()).map(PathBuf::from));
        // An isolated store must never inherit the host's saved credentials.
        if root.as_ref().is_some_and(|r| r != &directory.join("lore")) {
            return Ok(Self::default());
        }
        Self::from_file(&directory.join("settings.json"))
    }
    /// Load a caller-selected settings file without consulting or changing env.
    pub fn from_file(path: &Path) -> Result<Self> {
        let mut file = match crate::files::open_regular(path, crate::MAX_FRAME_BYTES) {
            Ok(file) => file,
            Err(Error::Unavailable) if !path.try_exists().unwrap_or(true) => return Ok(Self::default()),
            Err(error) => return Err(error),
        };
        let mut bytes = Vec::new();
        use std::io::Read;
        file.by_ref().take(crate::MAX_FRAME_BYTES as u64 + 1).read_to_end(&mut bytes)?;
        if bytes.len() > crate::MAX_FRAME_BYTES { return Err(Error::TooLarge); }
        let data: serde_json::Value = serde_json::from_slice(&bytes).map_err(|_| Error::InvalidRequest)?;
        if !data.is_object() { return Err(Error::InvalidRequest); }
        let mut saved = std::collections::HashMap::new();
        if let Some(values) = data.get("env") {
            let values = values.as_object().ok_or(Error::InvalidRequest)?;
            for (name, value) in values.iter().filter(|(name, _)| name.starts_with("LORE_")) {
                let value = value.as_str().ok_or(Error::InvalidRequest)?;
                saved.insert(name.clone(), value.to_owned());
            }
        }
        #[cfg(unix)] {
            use std::os::unix::fs::PermissionsExt;
            if saved.keys().any(|name| name.contains("KEY") || name.contains("TOKEN") || name.contains("SECRET"))
                && file.metadata()?.permissions().mode() & 0o077 != 0 {
                return Err(Error::UnsafePath);
            }
        }
        Ok(Self { saved })
    }
    /// Process overrides, including empty values, always win.
    pub fn get(&self, name: &str) -> std::result::Result<String, env::VarError> {
        match env::var(name) {
            Err(env::VarError::NotPresent) => self.saved.get(name).cloned().ok_or(env::VarError::NotPresent),
            value => value,
        }
    }
}
/// Environment-compatible shared lookup for standalone runtime consumers.
pub fn var(name: &str) -> std::result::Result<String, env::VarError> {
    match env::var(name) {
        Err(env::VarError::NotPresent) => ResolvedSettings::from_env().unwrap_or_default().get(name),
        value => value,
    }
}

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
    pub sync: SyncConfig,
}
#[derive(Clone)]
pub struct SyncConfig {
    pub enabled: bool,
    pub classes: std::collections::HashSet<String>,
    pub key: Option<String>,
}
impl std::fmt::Debug for SyncConfig {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("SyncConfig")
            .field("enabled", &self.enabled)
            .field("classes", &self.classes)
            .field("key_configured", &self.key.is_some())
            .finish()
    }
}
impl SyncConfig {
    pub fn from_env() -> Self {
        Self::from_settings(&ResolvedSettings::from_env().unwrap_or_default())
    }
    fn from_settings(saved: &ResolvedSettings) -> Self {
        let enabled = saved.get("LORE_DISABLE_SYNC").map_or(true, |s| s.is_empty() || s == "0");
        let classes = saved.get("LORE_SYNC_CLASSES")
            .unwrap_or_else(|_| "memory,filemap,beliefs,pending,skills,sessions".into())
            .split(',')
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(str::to_owned)
            .collect();
        Self {
            enabled,
            classes,
            key: saved.get("LORE_SYNC_HMAC_KEY")
                .ok()
                .filter(|s| !s.is_empty()),
        }
    }
}
impl Config {
    pub fn from_env(timeout: Duration) -> Result<Self> {
        Self::from_env_with_root(None, timeout)
    }
    pub fn from_env_with_root(effective_root: Option<PathBuf>, timeout: Duration) -> Result<Self> {
        let saved = ResolvedSettings::from_env_with_root(effective_root.as_deref())?;
        let home = env::var_os("HOME")
            .map(PathBuf::from)
            .ok_or(Error::Unavailable)?;
        let default_root = home.join(".claude/lore");
        let root = effective_root.or_else(|| env::var_os("LORE_ROOT")
            .filter(|s| !s.to_string_lossy().trim().is_empty())
            .map(PathBuf::from))
            .unwrap_or_else(|| default_root.clone());
        if !root.is_absolute() || timeout.is_zero() {
            return Err(Error::InvalidRequest);
        }
        let skills = env::var_os("LORE_SKILLS_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                if root == default_root {
                    home.join(".claude/skills")
                } else {
                    root.join("skills")
                }
            });
        let projects = env::var_os("LORE_PROJECTS_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|| home.join(".claude/projects"));
        let codex_home = env::var_os("CODEX_HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| home.join(".codex"));
        let codex_sessions = env::var_os("LORE_CODEX_SESSIONS_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|| codex_home.join("sessions"));
        Ok(Self {
            root,
            skills,
            projects,
            codex_sessions,
            user_cap: cap(&saved, "LORE_USER_CAP", 9000)?,
            project_cap: cap(&saved, "LORE_MEMORY_CAP", 8800)?,
            machine_cap: cap(&saved, "LORE_MACHINE_CAP", 4400)?,
            filemap_cap: cap(&saved, "LORE_FILEMAP_CAP", 4400)?,
            timeout,
            sync: SyncConfig::from_settings(&saved),
        })
    }
    pub fn settings(&self) -> Result<ResolvedSettings> {
        ResolvedSettings::from_env_with_root(Some(&self.root))
    }
    pub fn for_root(root: PathBuf) -> Self {
        Self {
            skills: root.join("skills"),
            projects: root.join("session-projects"),
            codex_sessions: root.join("codex-sessions"),
            root,
            user_cap: 9000,
            project_cap: 8800,
            machine_cap: 4400,
            filemap_cap: 4400,
            timeout: Duration::from_secs(3),
            sync: SyncConfig::from_settings(&ResolvedSettings::default()),
        }
    }
}
fn cap(saved: &ResolvedSettings, name: &str, default: usize) -> Result<usize> {
    match saved.get(name) {
        Ok(raw) => raw
            .trim()
            .parse::<usize>()
            .ok()
            .filter(|n| *n <= 1024 * 1024)
            .ok_or(Error::InvalidRequest),
        Err(env::VarError::NotPresent) => Ok(default),
        Err(_) => Err(Error::InvalidRequest),
    }
}
pub fn valid_slug(s: &str) -> bool {
    !s.is_empty()
        && s.chars().count() <= 255
        && s != "."
        && !s.contains("..")
        && !s.chars().any(|c| matches!(c, '/' | '\\' | '\0'))
}
pub fn valid_skill_name(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 128
        && s.split('-').all(|part| {
            !part.is_empty()
                && part
                    .bytes()
                    .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit())
        })
}
pub fn valid_id(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 128
        && s.bytes()
            .all(|c| c.is_ascii_alphanumeric() || c == b'-' || c == b'_')
}

/// Resolve standard Git layouts without launching a subprocess or hooks.
/// Linked worktrees share their main checkout's memory identity.
pub fn project_identity_root(cwd: &Path) -> PathBuf {
    for candidate in cwd.ancestors() {
        let git = candidate.join(".git");
        if git.is_dir() {
            return candidate.to_path_buf();
        }
        if let Ok(bytes) = crate::files::read_regular(&git, 4096) {
            if let Ok(text) = std::str::from_utf8(&bytes) {
                if let Some(path) = text.trim().strip_prefix("gitdir: ") {
                    let target = candidate.join(path);
                    if let Ok(target) = target.canonicalize() {
                        if target
                            .parent()
                            .and_then(Path::file_name)
                            .is_some_and(|p| p == "worktrees")
                        {
                            if let Some(root) = target
                                .parent()
                                .and_then(Path::parent)
                                .and_then(Path::parent)
                            {
                                if root.join(".git").is_dir() {
                                    return root.to_path_buf();
                                }
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
        if candidate
            .file_name()
            .and_then(|p| p.to_str())
            .is_some_and(|s| s.ends_with("worktree") || s.ends_with("worktrees"))
        {
            if let Some(root) = candidate.parent().filter(|r| r.join(".git").is_dir()) {
                return root.to_path_buf();
            }
        }
    }
    cwd.to_path_buf()
}
pub fn project_slug(cwd: &Path) -> String {
    project_identity_root(cwd)
        .to_string_lossy()
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() { c } else { '-' })
        .collect()
}
pub fn project_key(cwd: &Path) -> String {
    let root = project_identity_root(cwd);
    let config = root.join(".git/config");
    let Ok(raw) = crate::files::read_regular(&config, 64 * 1024) else {
        return project_slug(cwd);
    };
    let Ok(text) = std::str::from_utf8(&raw) else {
        return project_slug(cwd);
    };
    let mut origin = false;
    for line in text.lines().map(str::trim) {
        if line.starts_with('[') {
            origin = line == "[remote \"origin\"]";
            continue;
        }
        if origin {
            if let Some((key, value)) = line.split_once('=') {
                if key.trim() == "url" {
                    return normalize_origin(value.trim());
                }
            }
        }
    }
    project_slug(cwd)
}
pub fn normalize_origin(raw: &str) -> String {
    let raw = raw.trim();
    let raw = raw.split_once("://").map_or(raw, |(_, rest)| rest);
    let mut value = if let Some((_, host_path)) = raw.split_once('@') {
        host_path.replacen(':', "/", 1)
    } else {
        raw.to_owned()
    };
    if value.ends_with(".git") {
        value.truncate(value.len() - 4);
    }
    value.to_lowercase()
}

/// Canonical stage switch semantics shared with the compatible Python plugin.
pub fn disabled(name: &str) -> bool {
    var(name).is_ok_and(|value| !matches!(value.as_str(), "" | "0"))
}
pub fn runtime(cfg: &Config) -> serde_json::Value {
    let stages = [
        ("inject", "LORE_DISABLE_INJECT"),
        ("index", "LORE_DISABLE_INDEX"),
        ("review", "LORE_DISABLE_REVIEW"),
        ("beliefs", "LORE_DISABLE_BELIEFS"),
        ("skills", "LORE_DISABLE_SKILLS"),
    ];
    serde_json::json!({"root":cfg.root,"projects_dir":cfg.projects,"version":env!("CARGO_PKG_VERSION"),"disabled_stages":stages.into_iter().filter(|(_,name)|cfg.settings().is_ok_and(|s| s.get(name).is_ok_and(|v| !matches!(v.as_str(), "" | "0")))).map(|(stage,_)|stage).collect::<Vec<_>>()})
}

#[cfg(test)]
mod persisted_tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;
    fn settings(text: &str) -> (tempfile::TempDir, PathBuf) {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("settings.json");
        std::fs::write(&path, text).unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
        (dir, path)
    }
    #[test]
    fn saved_caps_signing_and_transport() {
        let (_dir, path) = settings(r#"{"env":{"LORE_MEMORY_CAP":"17600","LORE_FILEMAP_CAP":"8800","LORE_SYNC_HMAC_KEY":"fixture-key","LORE_SYNC_URL":"https://example.invalid","LORE_SYNC_TOKEN":"fixture-token","LORE_SYNC_CLASSES":"memory","LORE_DISABLE_SYNC":"1"}}"#);
        let saved = ResolvedSettings::from_file(&path).unwrap();
        assert_eq!(cap(&saved,"LORE_MEMORY_CAP",8800).unwrap(),17600);
        assert_eq!(cap(&saved,"LORE_FILEMAP_CAP",4400).unwrap(),8800);
        let sync = SyncConfig::from_settings(&saved);
        assert!(sync.key.is_some());
        assert!(!sync.enabled);
        assert!(sync.classes.contains("memory"));
        assert_eq!(saved.get("LORE_SYNC_URL").unwrap(),"https://example.invalid");
        assert!(saved.get("LORE_SYNC_TOKEN").is_ok());
    }
    #[test]
    fn rejects_malformed_and_unsafe_settings() {
        let (dir,path) = settings("{");
        assert!(matches!(ResolvedSettings::from_file(&path),Err(Error::InvalidRequest)));
        std::fs::write(&path,r#"{"env":{"LORE_SYNC_HMAC_KEY":"fixture"}}"#).unwrap();
        std::fs::set_permissions(&path,std::fs::Permissions::from_mode(0o644)).unwrap();
        assert!(matches!(ResolvedSettings::from_file(&path),Err(Error::UnsafePath)));
        std::fs::set_permissions(&path,std::fs::Permissions::from_mode(0o600)).unwrap();
        let link=dir.path().join("link.json");
        std::os::unix::fs::symlink(&path,&link).unwrap();
        assert!(matches!(ResolvedSettings::from_file(&link),Err(Error::UnsafePath)));
        std::fs::remove_file(&link).unwrap();
        std::fs::hard_link(&path,&link).unwrap();
        assert!(matches!(ResolvedSettings::from_file(&path),Err(Error::UnsafePath)));
    }
}
