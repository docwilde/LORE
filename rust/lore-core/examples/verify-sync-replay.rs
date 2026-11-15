#[path = "../src/sync_verification.rs"]
mod sync_verification;

use lore_core::{config::Config, Error, Result};
use std::{path::PathBuf, time::Duration};

fn run() -> Result<bool> {
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    let transport = args.first().is_some_and(|arg| arg == "--transport");
    if args.len() != if transport { 3 } else { 2 } {
        eprintln!("usage: verify-sync-replay RETAINED-BACKUP.db TEMPORARY-PARENT\n       verify-sync-replay --inspect-only RETAINED-BACKUP.db\n       verify-sync-replay --transport RETAINED-BACKUP.db TEMPORARY-PARENT\nTransport requires HTTP_PROXY/HTTPS_PROXY/ALL_PROXY and lowercase variants unset for this process.\nEmits scrubbed JSON; exit 0 means the requested mode's gates passed, 2 means evidence is incomplete.\nFull issue #112 success also requires contemporaneous file-state evidence.");
        return Err(Error::InvalidRequest);
    }
    let config = Config::from_env(Duration::from_secs(3))?;
    if transport {
        let report = sync_verification::verify_transport_backup(
            &PathBuf::from(&args[1]),
            &config,
            &PathBuf::from(&args[2]),
        )?;
        println!(
            "{}",
            serde_json::to_string_pretty(&report).map_err(|_| Error::Unavailable)?
        );
        return Ok(report["transport_replay_gate"] == true);
    }
    if args[0] == "--inspect-only" {
        let report = sync_verification::inspect_backup(&PathBuf::from(&args[1]), &config)?;
        println!(
            "{}",
            serde_json::to_string_pretty(&report).map_err(|_| Error::Unavailable)?
        );
        return Ok(report["signature_gate"] == true && report["source"]["unchanged"] == true);
    }
    let report = sync_verification::verify_backup(
        &PathBuf::from(&args[0]),
        &config,
        &PathBuf::from(&args[1]),
    )?;
    println!(
        "{}",
        serde_json::to_string_pretty(&report).map_err(|_| Error::Unavailable)?
    );
    Ok(report["offline_replay_gate"] == true)
}

fn main() {
    let status = match run() {
        Ok(true) => 0,
        Ok(false) => 2,
        Err(error) => {
            eprintln!("replay verification refused ({})", error.code());
            1
        }
    };
    std::process::exit(status);
}
