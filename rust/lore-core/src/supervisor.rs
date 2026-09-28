//! Standalone paid work owns a retained process group and orphan reaper.
//! The retained worker PID prevents process-group reuse during cleanup.
use crate::{Error, Result};
use std::{
    process::{Command, Stdio},
    sync::atomic::{AtomicBool, Ordering},
    time::{Duration, Instant},
};
static CANCELLED: AtomicBool = AtomicBool::new(false);
extern "C" fn cancel(_: libc::c_int) {
    CANCELLED.store(true, Ordering::Relaxed);
}
extern "C" fn worker_parent_lost(_: libc::c_int) {
    unsafe {
        libc::kill(-libc::getpgrp(), libc::SIGKILL);
    }
}
pub fn required(args: &[String]) -> bool {
    matches!(
        args.first().map(String::as_str),
        Some("review" | "backfill" | "dream")
    ) || args.first().is_some_and(|a| a == "graph") && args.get(1).is_some_and(|a| a == "derive")
        || args.first().is_some_and(|a| a == "hook")
            && args.windows(2).any(|a| {
                a[0] == "--event" && matches!(a[1].as_str(), "pre-compact" | "session-end")
            })
}
#[cfg(target_os = "linux")]
pub fn enter_worker() -> Result<bool> {
    let Some(raw) = std::env::var_os("LORE_NATIVE_SUPERVISOR_PID") else {
        return Ok(false);
    };
    let pid = raw
        .to_str()
        .and_then(|s| s.parse::<i32>().ok())
        .ok_or(Error::Untrusted)?;
    if unsafe { libc::getppid() } != pid || unsafe { libc::getpgrp() } != unsafe { libc::getpid() }
    {
        return Err(Error::Untrusted);
    }
    unsafe {
        libc::signal(
            libc::SIGTERM,
            worker_parent_lost as *const () as libc::sighandler_t,
        );
        libc::signal(
            libc::SIGINT,
            worker_parent_lost as *const () as libc::sighandler_t,
        );
        libc::signal(
            libc::SIGHUP,
            worker_parent_lost as *const () as libc::sighandler_t,
        );
    }
    if unsafe { libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGTERM) } != 0 {
        return Err(Error::Untrusted);
    }
    if unsafe { libc::getppid() } != pid {
        unsafe {
            libc::kill(-libc::getpgrp(), libc::SIGKILL);
        }
    }
    std::env::remove_var("LORE_NATIVE_SUPERVISOR_PID");
    Ok(true)
}
#[cfg(target_os = "linux")]
pub fn run(args: &[String]) -> Result<i32> {
    use std::os::unix::process::CommandExt;
    if unsafe { libc::prctl(libc::PR_SET_CHILD_SUBREAPER, 1) } != 0 {
        return Err(Error::Untrusted);
    }
    unsafe {
        libc::signal(libc::SIGTERM, cancel as *const () as libc::sighandler_t);
        libc::signal(libc::SIGINT, cancel as *const () as libc::sighandler_t);
        libc::signal(libc::SIGHUP, cancel as *const () as libc::sighandler_t);
    }
    let parent = unsafe { libc::getpid() };
    let mut command = Command::new(std::env::current_exe()?);
    command
        .args(args)
        .env("LORE_NATIVE_SUPERVISOR_PID", parent.to_string())
        .stdin(Stdio::inherit())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit());
    unsafe {
        command.pre_exec(move || {
            if libc::setpgid(0, 0) != 0 || libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGTERM) != 0 {
                return Err(std::io::Error::last_os_error());
            }
            if libc::getppid() != parent {
                return Err(std::io::Error::other("supervisor exited"));
            }
            Ok(())
        });
    }
    let mut child = command.spawn()?;
    let pid = child.id() as libc::pid_t;
    let hook = args.first().is_some_and(|s| s == "hook");
    let deadline = Instant::now() + Duration::from_secs(if hook { 25 } else { 3600 });
    let mut cancelled = false;
    let observation = (|| -> Result<()> {
        loop {
            let mut info = unsafe { std::mem::zeroed::<libc::siginfo_t>() };
            let result = unsafe {
                libc::waitid(
                    libc::P_PID,
                    pid as libc::id_t,
                    &mut info,
                    libc::WEXITED | libc::WNOHANG | libc::WNOWAIT,
                )
            };
            if result != 0 {
                return Err(Error::Unavailable);
            }
            if unsafe { info.si_pid() } == pid {
                return Ok(());
            }
            if CANCELLED.load(Ordering::Relaxed) || Instant::now() >= deadline {
                cancelled = true;
                return Ok(());
            }
            std::thread::sleep(Duration::from_millis(10));
        }
    })();
    // Kill before reaping the group leader, even after normal completion.
    unsafe {
        libc::kill(-pid, libc::SIGKILL);
    }
    let status = child.wait();
    // Subreaper ownership admits escaped provider grandchildren too. Reparented
    // children are our children, so none can be an unrelated reused PID.
    let children = std::path::PathBuf::from(format!("/proc/{parent}/task/{parent}/children"));
    let cleanup_deadline = Instant::now() + Duration::from_secs(3);
    loop {
        let raw = std::fs::read_to_string(&children).unwrap_or_default();
        let ids = raw
            .split_whitespace()
            .filter_map(|s| s.parse::<libc::pid_t>().ok())
            .take(4096)
            .collect::<Vec<_>>();
        if ids.is_empty() {
            break;
        }
        for id in ids {
            unsafe {
                libc::kill(id, libc::SIGKILL);
            }
            let mut st = 0;
            unsafe {
                libc::waitpid(id, &mut st, libc::WNOHANG);
            }
        }
        if Instant::now() >= cleanup_deadline {
            return Err(Error::MayHaveApplied);
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    observation?;
    if cancelled {
        return Err(Error::MayHaveApplied);
    }
    Ok(status?.code().unwrap_or(1))
}
#[cfg(not(target_os = "linux"))]
pub fn enter_worker() -> Result<bool> {
    Ok(false)
}
#[cfg(not(target_os = "linux"))]
pub fn run(_args: &[String]) -> Result<i32> {
    Err(Error::Unsupported)
}
