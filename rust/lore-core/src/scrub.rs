//! Native ingestion choke point. A matcher failure refuses data, never leaks it.
use crate::{Error, Result, MAX_FRAME_BYTES};
use fancy_regex::{Captures, Regex, RegexBuilder};
use serde::Deserialize;
use std::sync::OnceLock;

#[derive(Deserialize)]
struct Pattern {
    kind: String,
    pattern: String,
    flags: u32,
}
static PATTERNS: OnceLock<Result<Vec<(String, Regex)>>> = OnceLock::new();
fn patterns() -> Result<&'static Vec<(String, Regex)>> {
    PATTERNS
        .get_or_init(|| {
            let source: Vec<Pattern> = serde_json::from_str(include_str!("scrub_patterns.json"))
                .map_err(|_| Error::Unavailable)?;
            source
                .into_iter()
                .map(|p| {
                    let flags = match (p.flags & 2 != 0, p.flags & 16 != 0) {
                        (true, true) => "(?is)",
                        (true, false) => "(?i)",
                        (false, true) => "(?s)",
                        _ => "",
                    };
                    let regex =
                        RegexBuilder::new(&format!("{flags}{}", p.pattern.replace("\\Z", "\\z")))
                            .backtrack_limit(1_000_000)
                            .build()
                            .map_err(|_| Error::Unavailable)?;
                    Ok((p.kind, regex))
                })
                .collect()
        })
        .as_ref()
        .map_err(|e| *e)
}
fn pointer(value: &str) -> bool {
    let lower = value.to_lowercase();
    let reference = value
        .strip_prefix("op://")
        .or_else(|| lower.strip_prefix("vault://"))
        .or_else(|| lower.strip_prefix("vault:"))
        .or_else(|| lower.strip_prefix("keyring://"));
    if let Some(tail) = reference {
        return !tail.is_empty()
            && !tail
                .chars()
                .any(|c| c.is_whitespace() || matches!(c, ',' | '='));
    }
    let variable = value
        .strip_prefix("${")
        .and_then(|s| s.strip_suffix('}'))
        .or_else(|| value.strip_prefix('$'));
    if let Some(variable) = variable {
        return variable
            .as_bytes()
            .first()
            .is_some_and(|c| c.is_ascii_alphabetic() || *c == b'_')
            && variable
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || c == b'_');
    }
    value
        .strip_prefix('<')
        .and_then(|s| s.strip_suffix('>'))
        .is_some_and(|s| {
            !s.is_empty()
                && !s
                    .chars()
                    .any(|c| c.is_whitespace() || matches!(c, '<' | '>'))
        })
}
fn group<'a>(caps: &Captures<'a, str>, index: usize) -> Result<&'a str> {
    caps.get(index)
        .map(|m| m.as_str())
        .ok_or(Error::Unavailable)
}
fn replacement(kind: &str, caps: &Captures<'_, str>, text: &str) -> Result<String> {
    let matched = caps.get(0).ok_or(Error::Unavailable)?;
    let value = matched.as_str();
    match kind {
        "KV_SECRET_QUOTED" => {
            let key = group(caps, 1)?;
            let sep = group(caps, 2)?;
            let quote = group(caps, 3)?;
            let value = group(caps, 4)?;
            Ok(if value.trim().chars().count() < 8 || pointer(value) {
                matched.as_str().into()
            } else {
                format!("{key}{sep}{quote}[REDACTED:value]{quote}")
            })
        }
        "KV_SECRET" => {
            let key = group(caps, 1)?;
            let sep = group(caps, 2)?;
            let value = group(caps, 3)?;
            if pointer(value) {
                return Ok(matched.as_str().into());
            }
            // Signed historical notes may already contain a scrub marker
            // followed by punctuation. Replaying them must not append another
            // closing bracket on every receiver pass.
            if value
                .strip_prefix("[REDACTED:")
                .and_then(|rest| rest.split_once(']'))
                .is_some_and(|(marker_kind, tail)| {
                    let generated = matches!(marker_kind, "value" | "hex" | "base64")
                        || patterns().is_ok_and(|known| {
                            known.iter().any(|(name, _)| name == marker_kind)
                        });
                    generated && tail.chars().all(|c| "\"'`,;)]}>".contains(c))
                })
            {
                return Ok(matched.as_str().into());
            }
            let body = value.trim_end_matches(|c| "\"'`,;)]}>".contains(c));
            Ok(if body.is_empty() || pointer(body) {
                matched.as_str().into()
            } else {
                format!("{key}{sep}[REDACTED:value]{}", &value[body.len()..])
            })
        }
        "HEX_RUN" => Ok("[REDACTED:hex]".into()),
        "BASE64_RUN" => {
            let fingerprint = value.len() == 43
                && !value.contains('=')
                && matched.start() >= 7
                && text.get(matched.start() - 7..matched.start()) == Some("SHA256:");
            let path = !value.contains(['+', '='])
                && value.contains('/')
                && value.split('/').map(str::len).max().unwrap_or(0) < 16;
            Ok(if fingerprint || path {
                value.into()
            } else {
                "[REDACTED:base64]".into()
            })
        }
        kind => Ok(format!("[REDACTED:{kind}]")),
    }
}
pub fn scrub(text: &str) -> Result<String> {
    if text.len() > MAX_FRAME_BYTES {
        return Err(Error::TooLarge);
    }
    let mut text = text.to_owned();
    for (kind, regex) in patterns()? {
        let mut out = String::new();
        let mut offset = 0;
        for captures in regex.captures_iter(text.as_str()) {
            let captures = captures.map_err(|_| Error::Unavailable)?;
            let matched = captures.get(0).ok_or(Error::Unavailable)?;
            out.push_str(&text[offset..matched.start()]);
            out.push_str(&replacement(kind, &captures, &text)?);
            offset = matched.end();
            if out.len() > MAX_FRAME_BYTES {
                return Err(Error::TooLarge);
            }
        }
        out.push_str(&text[offset..]);
        if out.len() > MAX_FRAME_BYTES {
            return Err(Error::TooLarge);
        }
        text = out;
    }
    Ok(text)
}
pub fn scrub_json(value: &serde_json::Value) -> Result<serde_json::Value> {
    use serde_json::Value;
    fn walk(value: &Value, depth: usize) -> Result<Value> {
        if depth > 32 {
            return Err(Error::TooLarge);
        }
        Ok(match value {
            Value::String(s) => Value::String(scrub(s)?),
            Value::Array(a) => Value::Array(
                a.iter()
                    .map(|v| walk(v, depth + 1))
                    .collect::<Result<_>>()?,
            ),
            Value::Object(o) => Value::Object(
                o.iter()
                    .map(|(k, v)| Ok((k.clone(), walk(v, depth + 1)?)))
                    .collect::<Result<_>>()?,
            ),
            _ => value.clone(),
        })
    }
    if serde_json::to_vec(value)
        .map_err(|_| Error::InvalidRequest)?
        .len()
        > MAX_FRAME_BYTES
    {
        return Err(Error::TooLarge);
    }
    walk(value, 0)
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn redacts_credentials_but_preserves_references_and_public_fingerprint() {
        let text="password='long secret phrase' token=${MY_TOKEN} token=op://vault/item/field ghp_abcdefghijklmnopqrstuvwxyz0123456789";
        let clean = scrub(text).unwrap();
        assert!(clean.contains("password='[REDACTED:value]'"));
        assert!(clean.contains("token=${MY_TOKEN}"));
        assert!(clean.contains("token=op://vault/item/field"));
        assert!(!clean.contains("ghp_"));
        let fingerprint = format!("SHA256:{}", "g".repeat(42) + "+");
        assert_eq!(scrub(&fingerprint).unwrap(), fingerprint);
    }

    #[test]
    fn replay_of_an_existing_redaction_does_not_grow_brackets() {
        let already = "token=[REDACTED:value]] and password=[REDACTED:value]";
        assert_eq!(scrub(already).unwrap(), already);
        let first = scrub("token=unmistakably-secret]").unwrap();
        assert_eq!(scrub(&first).unwrap(), first);
        assert_eq!(
            scrub("token=[REDACTED:NeverShareThisSecret]").unwrap(),
            "token=[REDACTED:value]]"
        );
    }
}
