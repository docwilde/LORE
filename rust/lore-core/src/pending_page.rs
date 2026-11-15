//! Bounded pending reads. Continuations bind one directory revision and scope;
//! a page never grants approval authority or claims a complete global count.
use crate::{
    config::{self, Config},
    files, gate, pending, Error, Result,
};
use hmac::{Hmac, Mac};
use serde_json::{json, Value};
use sha2::Sha256;
use std::{
    path::Path,
    time::{SystemTime, UNIX_EPOCH},
};

const SCAN_LIMIT: usize = 256;
const ITEMS_BYTES: usize = 40 * 1024;
const CURSOR_BYTES: usize = 2048;
const CURSOR_LIFETIME: u64 = 900;

fn now() -> Result<u64> {
    Ok(SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| Error::Unavailable)?
        .as_secs())
}
fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}
fn unhex(text: &str) -> Result<Vec<u8>> {
    if text.len() % 2 != 0
        || !text
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err(Error::InvalidRequest);
    }
    (0..text.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&text[i..i + 2], 16).map_err(|_| Error::InvalidRequest))
        .collect()
}
fn cursor_key(cfg: &Config, create: bool) -> Result<Vec<u8>> {
    let path = cfg.root.join(".pending-page-key");
    if !path.try_exists()? && create {
        let mut bytes = uuid::Uuid::new_v4().as_bytes().to_vec();
        bytes.extend_from_slice(uuid::Uuid::new_v4().as_bytes());
        files::atomic_write(&path, &bytes)?;
    }
    let key = files::read_regular(&path, 32)?;
    if key.len() != 32 {
        return Err(Error::Unavailable);
    }
    Ok(key)
}
fn encode(cfg: &Config, value: &Value) -> Result<String> {
    let payload = serde_json::to_vec(value).map_err(|_| Error::Unavailable)?;
    let mut mac =
        Hmac::<Sha256>::new_from_slice(&cursor_key(cfg, true)?).map_err(|_| Error::Unavailable)?;
    mac.update(&payload);
    let cursor = format!("{}.{}", hex(&payload), hex(&mac.finalize().into_bytes()));
    if cursor.len() > CURSOR_BYTES {
        return Err(Error::TooLarge);
    }
    Ok(cursor)
}
fn decode(cfg: &Config, text: &str) -> Result<Value> {
    if text.len() > CURSOR_BYTES {
        return Err(Error::InvalidRequest);
    }
    let (payload, signature) = text.split_once('.').ok_or(Error::InvalidRequest)?;
    let payload = unhex(payload)?;
    let signature = unhex(signature)?;
    let mut mac =
        Hmac::<Sha256>::new_from_slice(&cursor_key(cfg, false)?).map_err(|_| Error::Unavailable)?;
    mac.update(&payload);
    mac.verify_slice(&signature)
        .map_err(|_| Error::InvalidRequest)?;
    serde_json::from_slice(&payload).map_err(|_| Error::InvalidRequest)
}
#[cfg(unix)]
fn revision(meta: &std::fs::Metadata) -> Value {
    use std::os::unix::fs::MetadataExt;
    json!([
        meta.dev(),
        meta.ino(),
        meta.mtime(),
        meta.mtime_nsec(),
        meta.ctime(),
        meta.ctime_nsec()
    ])
}

pub fn list(cfg: &Config, req: &Value) -> Result<Value> {
    #[cfg(not(unix))]
    {
        let _ = (cfg, req);
        Err(Error::Unsupported)
    }
    #[cfg(unix)]
    {
        let slug = config::project_slug(gate::cwd(req)?);
        let scope = crate::digest(slug.as_bytes());
        let limit = req
            .get("limit")
            .map_or(Some(50), Value::as_u64)
            .filter(|n| (1..=50).contains(n))
            .ok_or(Error::InvalidRequest)? as usize;
        let cursor = match req.get("cursor") {
            None => None,
            Some(value) => Some(value.as_str().ok_or(Error::InvalidRequest)?),
        };
        let path = cfg.root.join("pending");
        let missing = match std::fs::symlink_metadata(&path) {
            Ok(_) => false,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => true,
            Err(error) => return Err(error.into()),
        };
        if missing {
            if cursor.is_some() {
                return Err(Error::Changed);
            }
            return Ok(json!({"items":[],"next_cursor":null,"has_more":false,
                "page_complete":true,"unavailable_count":0,"unavailable":[],"scanned":0}));
        }
        let _namespace = pending::namespace_lock(cfg)?;
        let mut stream = files::DirectoryStream::open(&path)?;
        let original = revision(&stream.metadata()?);
        if let Some(cursor) = cursor {
            let state = decode(cfg, cursor)?;
            if state["version"] != 2
                || state["scope"] != scope
                || state["revision"] != original
                || state["expires"]
                    .as_u64()
                    .is_none_or(|expires| expires < now().unwrap_or(u64::MAX))
            {
                return Err(Error::Changed);
            }
            stream.seek(state["position"].as_i64().ok_or(Error::InvalidRequest)?)?;
        }
        let mut items = Vec::new();
        let mut unavailable = Vec::new();
        let mut unavailable_count = 0;
        let mut scanned = 0;
        let mut bytes = 0;
        let mut exhausted = false;
        let mut resume = stream.position()?;
        while scanned < SCAN_LIMIT && items.len() < limit {
            let before = stream.position()?;
            let Some(name) = stream.next()? else {
                exhausted = true;
                break;
            };
            scanned += 1;
            resume = stream.position()?;
            let entry = Path::new(&name);
            if entry.extension().is_none_or(|s| s != "json") && name != ".json" {
                continue;
            }
            let id = entry
                .file_stem()
                .and_then(|s| s.to_str())
                .filter(|id| config::valid_id(id));
            let result = match id {
                None => Err(Error::InvalidRequest),
                Some(id) => pending::snapshot_at(stream.directory(), &name).and_then(|snap| {
                    if !pending::visible(&snap.item, &slug) {
                        return Ok(None);
                    }
                    pending::display_row(id, &snap.item).map(Some)
                }),
            };
            match result {
                Ok(Some(row)) => {
                    let size = serde_json::to_vec(&row)
                        .map_err(|_| Error::Unavailable)?
                        .len();
                    if size > ITEMS_BYTES {
                        unavailable_count += 1;
                        if unavailable.len() < 8 {
                            unavailable.push(json!({"pid":id,"error":"output_too_large"}));
                        }
                    } else if bytes + size > ITEMS_BYTES {
                        resume = before;
                        break;
                    } else {
                        bytes += size;
                        items.push(row);
                    }
                }
                Ok(None) => {}
                Err(error) => {
                    unavailable_count += 1;
                    if unavailable.len() < 8 {
                        unavailable.push(json!({"pid":id,"error":error.code()}));
                    }
                }
            }
        }
        if revision(&stream.metadata()?) != original
            || revision(&files::open_directory(&path)?.metadata()?) != original
        {
            return Err(Error::Changed);
        }
        let next = if exhausted {
            Value::Null
        } else {
            json!(encode(
                cfg,
                &json!({
                    "version":2,"scope":scope,"revision":original,"position":resume,"expires":now()?+CURSOR_LIFETIME
                })
            )?)
        };
        Ok(
            json!({"items":items,"next_cursor":next,"has_more":!exhausted,
            "page_complete":unavailable_count==0,"unavailable_count":unavailable_count,
            "unavailable":unavailable,"scanned":scanned}),
        )
    }
}
