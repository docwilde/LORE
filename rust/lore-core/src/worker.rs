//! One supervised review. All children inherit the supervisor's owned process
//! group; bounded pipes and a deadline prevent provider output or stdin stalls.
use crate::{config::Config, gate::Authority, Error, Result};
use serde_json::Value;
use std::{
    io::{Read, Write},
    process::{Command, Stdio},
    sync::mpsc,
    thread,
    time::{Duration, Instant},
};

const OUTPUT_CAP: usize = 1024 * 1024;
pub fn run(cfg: &Config, req: &Value, engine: &str) -> Result<()> {
    if !matches!(engine, "claude" | "codex" | "deepseek" | "glm") {
        return Err(Error::InvalidRequest);
    }
    #[cfg(unix)]
    if unsafe { libc::getpgrp() } != unsafe { libc::getpid() } {
        return Err(Error::Untrusted);
    }
    #[cfg(not(unix))]
    return Err(Error::Unsupported);
    let authority = Authority::Derived {
        agent: "doxa-deriver".into(),
        engine: engine.into(),
    };
    let Some(job) = crate::review::build_review_job(cfg, req, &authority)? else {
        return Ok(());
    };
    let program = std::env::var("LORE_CLAUDE_BIN")
        .ok()
        .filter(|s| !s.trim().is_empty())
        .unwrap_or_else(|| "claude".into());
    let deadline = Instant::now() + Duration::from_secs(150);
    let response = review_provider(
        &program,
        "LORE_DERIVER_MODEL",
        "haiku",
        job.prompt(),
        deadline,
    )?;
    let result = crate::review::process_result(cfg, &job, &response)?;
    let deferred =
        std::env::var("LORE_DEFER_DREAM").is_ok_and(|value| !matches!(value.as_str(), "" | "0"));
    if result["beliefs"]["derived"].as_u64().unwrap_or(0) > 0 && !deferred {
        // Earlier review effects have landed. A reconciliation error must not
        // advertise a safe retry of the complete review.
        let reconcile = (|| {
            let cwd = crate::gate::cwd(req)?;
            if let Some(dream) = crate::dream::build(cfg, cwd, &authority)? {
                let response = review_provider(
                    &program,
                    "LORE_DREAMER_MODEL",
                    "sonnet",
                    dream.prompt(),
                    deadline,
                )?;
                crate::dream::process(cfg, &dream, &response)?;
            }
            Ok::<(), Error>(())
        })();
        reconcile.map_err(|_| Error::MayHaveApplied)?;
    }
    Ok(())
}

fn review_provider(
    program: &str,
    model_env: &str,
    fallback: &str,
    prompt: &str,
    deadline: Instant,
) -> Result<String> {
    let model = std::env::var(model_env)
        .ok()
        .filter(|s| !s.is_empty())
        .or_else(|| {
            std::env::var("LORE_REVIEW_MODEL")
                .ok()
                .filter(|s| !s.is_empty())
        })
        .unwrap_or_else(|| fallback.into());
    if model.len() > 128 || model.starts_with('-') || model.chars().any(char::is_control) {
        return Err(Error::InvalidRequest);
    }
    let mut response = provider(program, &model, prompt, true, deadline)?;
    // Retry only an explicit pre-model authentication refusal: --bare may
    // skip stored OAuth. General failures must not duplicate model work.
    if !response.success
        && format!("{}{}", response.out, response.err)
            .to_lowercase()
            .contains("not logged in")
    {
        response = provider(program, &model, prompt, false, deadline)?;
    }
    if !response.success {
        return Err(Error::Unavailable);
    }
    Ok(response.out)
}

struct Response {
    success: bool,
    out: String,
    err: String,
}
enum Pipe {
    Stdout(std::io::Result<Vec<u8>>),
    Stderr(std::io::Result<Vec<u8>>),
    Input(std::io::Result<()>),
}
fn collect(mut pipe: impl Read, cap: usize) -> std::io::Result<Vec<u8>> {
    let mut bytes = Vec::new();
    pipe.by_ref().take(cap as u64 + 1).read_to_end(&mut bytes)?;
    Ok(bytes)
}
fn provider(
    program: &str,
    model: &str,
    prompt: &str,
    bare: bool,
    deadline: Instant,
) -> Result<Response> {
    let mut command = Command::new(program);
    if bare {
        command.arg("--bare");
    }
    command
        .args(["-p", "--model", model, "--allowedTools", ""])
        .env("LORE_SKIP", "1")
        .env("LORE_DISABLE_REVIEW", "1")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = command.spawn()?;
    let stdout = child.stdout.take().ok_or(Error::Unavailable)?;
    let stderr = child.stderr.take().ok_or(Error::Unavailable)?;
    let mut stdin = child.stdin.take().ok_or(Error::Unavailable)?;
    let bytes = prompt.as_bytes().to_vec();
    let (tx, rx) = mpsc::channel();
    let out_tx = tx.clone();
    let err_tx = tx.clone();
    thread::spawn(move || {
        let _ = out_tx.send(Pipe::Stdout(collect(stdout, OUTPUT_CAP)));
    });
    thread::spawn(move || {
        let _ = err_tx.send(Pipe::Stderr(collect(stderr, 256 * 1024)));
    });
    thread::spawn(move || {
        let _ = tx.send(Pipe::Input(stdin.write_all(&bytes)));
    });
    let outcome = (|| {
        let mut out = None;
        let mut err = None;
        let mut input = false;
        let mut status = None;
        loop {
            if Instant::now() >= deadline {
                return Err(Error::Timeout);
            }
            while let Ok(pipe) = rx.try_recv() {
                match pipe {
                    Pipe::Stdout(value) => {
                        let bytes = value?;
                        if bytes.len() > OUTPUT_CAP {
                            return Err(Error::TooLarge);
                        }
                        out = Some(String::from_utf8(bytes).map_err(|_| Error::InvalidRequest)?);
                    }
                    Pipe::Stderr(value) => {
                        let bytes = value?;
                        if bytes.len() > 256 * 1024 {
                            return Err(Error::TooLarge);
                        }
                        err = Some(String::from_utf8_lossy(&bytes).into_owned());
                    }
                    Pipe::Input(value) => {
                        value?;
                        input = true;
                    }
                }
            }
            if status.is_none() {
                status = child.try_wait()?;
            }
            if input && out.is_some() && err.is_some() {
                if let Some(status) = status {
                    return Ok(Response {
                        success: status.success(),
                        out: out.take().unwrap(),
                        err: err.take().unwrap(),
                    });
                }
            }
            thread::sleep(Duration::from_millis(10));
        }
    })();
    if child.try_wait().ok().flatten().is_none() {
        let _ = child.kill();
    }
    let _ = child.wait();
    // The outer EOF supervisor retains the native group leader with WNOWAIT
    // until every provider descendant is killed and reaped on all exit paths.
    outcome
}
