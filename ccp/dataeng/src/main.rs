//! `ccp-engine` — the data engine's command line surface.
//!
//! This binary is the boundary the Python control plane drives. It speaks JSON on
//! stdout (`--json`) so orchestration never parses human text, and it is
//! deliberately thin: every command resolves arguments, calls into the library,
//! and reports. Storage policy lives in Julia, storage mechanics in the library,
//! user-facing workflow in Python.

use std::path::PathBuf;
use std::process::ExitCode;

use ccp_dataeng::error::{CcpError, Result};
use ccp_dataeng::json::Json;
use ccp_dataeng::sha256;
use ccp_dataeng::store::{self, Repository, DEFAULT_BLOCK_SIZE};
use ccp_dataeng::strategy::StrategyEngine;
use ccp_dataeng::{bitexec, format};

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match run(&args) {
        Ok(output) => {
            println!("{output}");
            ExitCode::SUCCESS
        }
        Err(e) => {
            // Errors go to stderr as JSON too: the control plane must be able to
            // tell an integrity failure from a missing file without regex.
            let kind = match &e {
                CcpError::Integrity(_) => "integrity",
                CcpError::Format(_) | CcpError::UnsupportedVersion(_) => "format",
                CcpError::Strategy(_) => "strategy",
                CcpError::NotFound(_) => "not_found",
                CcpError::Invalid(_) => "invalid",
                CcpError::Io(_) => "io",
            };
            eprintln!(
                "{}",
                Json::obj(vec![
                    ("ok", Json::Bool(false)),
                    ("error", Json::str(kind)),
                    ("message", Json::str(e.to_string())),
                ])
                .to_string_compact()
            );
            // A distinct code for integrity failures: it is the one error that
            // means "stored data is wrong", not "you asked for the wrong thing".
            match e {
                CcpError::Integrity(_) => ExitCode::from(3),
                _ => ExitCode::FAILURE,
            }
        }
    }
}

fn run(args: &[String]) -> Result<String> {
    let command = args.first().map(|s| s.as_str()).unwrap_or("help");
    match command {
        "store" => cmd_store(&args[1..]),
        "reconstruct" => cmd_reconstruct(&args[1..]),
        "list" => cmd_list(&args[1..]),
        "inspect" => cmd_inspect(&args[1..]),
        "verify" => cmd_verify(&args[1..]),
        "engine-info" => Ok(Json::obj(vec![
            ("ok", Json::Bool(true)),
            ("dataeng_version", Json::str(ccp_dataeng::VERSION)),
            ("bitexec", Json::str(bitexec::version())),
            (
                "bitexec_features",
                Json::Array(bitexec::feature_names().into_iter().map(Json::str).collect()),
            ),
            ("container_format_version", Json::uint(format::FORMAT_VERSION as u64)),
            ("default_block_size", Json::uint(DEFAULT_BLOCK_SIZE)),
        ])
        .to_string_pretty()),
        "help" | "--help" | "-h" => Ok(USAGE.to_string()),
        other => Err(CcpError::Invalid(format!("unknown command {other:?}; try --help"))),
    }
}

const USAGE: &str = "\
ccp-engine — CCP data and storage engine

  store       --repo DIR --file PATH --name NAME [--base NAME] [--block-size N]
  reconstruct --repo DIR --version NAME --out PATH
  list        --repo DIR
  inspect     --repo DIR --version NAME
  verify      --repo DIR [--version NAME]
  engine-info

Every command prints JSON. Errors print JSON on stderr; exit code 3 means an
integrity failure specifically.";

// ---- argument handling ----------------------------------------------------

fn flag<'a>(args: &'a [String], name: &str) -> Option<&'a str> {
    let mut it = args.iter();
    while let Some(a) = it.next() {
        if a == name {
            return it.next().map(|s| s.as_str());
        }
        if let Some(rest) = a.strip_prefix(&format!("{name}=")) {
            return Some(rest);
        }
    }
    None
}

fn required<'a>(args: &'a [String], name: &str) -> Result<&'a str> {
    flag(args, name).ok_or_else(|| CcpError::Invalid(format!("{name} is required")))
}

fn repo(args: &[String]) -> Result<Repository> {
    Repository::open_or_create(PathBuf::from(required(args, "--repo")?))
}

// ---- commands ------------------------------------------------------------

fn cmd_store(args: &[String]) -> Result<String> {
    let mut repository = repo(args)?;
    let file = PathBuf::from(required(args, "--file")?);
    let name = required(args, "--name")?;
    let base = flag(args, "--base");
    let block_size = match flag(args, "--block-size") {
        Some(v) => v
            .parse::<u64>()
            .map_err(|_| CcpError::Invalid(format!("--block-size {v:?} is not a number")))?,
        None => DEFAULT_BLOCK_SIZE,
    };

    let strategy = StrategyEngine::discover()?;
    let outcome = repository.store(&file, name, base, block_size, &strategy)?;

    let blocks: Vec<Json> = outcome
        .blocks
        .iter()
        .map(|b| {
            Json::obj(vec![
                ("index", Json::uint(b.index)),
                ("kind", Json::str(b.kind.as_str())),
                ("logical_len", Json::uint(b.logical_len)),
                ("payload_len", Json::uint(b.payload_len)),
                ("changed_bits", Json::uint(b.changed_bits)),
                ("changed_bytes", Json::uint(b.changed_bytes)),
                ("reason", Json::str(&b.reason)),
            ])
        })
        .collect();

    let r = &outcome.record;
    Ok(Json::obj(vec![
        ("ok", Json::Bool(true)),
        ("id", Json::str(sha256::to_hex(&r.id))),
        ("name", Json::str(&r.name)),
        (
            "base",
            match &outcome.base_name {
                Some(b) => Json::str(b),
                None => Json::Null,
            },
        ),
        ("artifact_size", Json::uint(r.size)),
        ("stored_size", Json::uint(r.stored_size)),
        ("content_sha256", Json::str(sha256::to_hex(&r.content_sha256))),
        ("chain_depth", Json::uint(r.chain_depth)),
        ("block_size", Json::uint(r.block_size)),
        ("block_count", Json::uint(outcome.blocks.len() as u64)),
        ("changed_bits", Json::uint(outcome.changed_bits)),
        ("changed_bytes", Json::uint(outcome.changed_bytes)),
        // Reported, never asserted as a general claim: this is the ratio for this
        // one version against storing it in full.
        (
            "stored_over_full",
            Json::num(if outcome.full_size > 0 {
                r.stored_size as f64 / outcome.full_size as f64
            } else {
                0.0
            }),
        ),
        (
            "bitexec_features",
            Json::Array(outcome.bitexec_features.iter().map(|s| Json::str(*s)).collect()),
        ),
        ("blocks", Json::Array(blocks)),
    ])
    .to_string_pretty())
}

fn cmd_reconstruct(args: &[String]) -> Result<String> {
    let repository = repo(args)?;
    let version = required(args, "--version")?;
    let out = PathBuf::from(required(args, "--out")?);
    let record = repository.reconstruct(version, &out)?;
    Ok(Json::obj(vec![
        ("ok", Json::Bool(true)),
        ("name", Json::str(&record.name)),
        ("out", Json::str(out.display().to_string())),
        ("size", Json::uint(record.size)),
        ("content_sha256", Json::str(sha256::to_hex(&record.content_sha256))),
        ("chain_depth", Json::uint(record.chain_depth)),
        // Reaching this line at all means the digest matched: reconstruct returns
        // an error rather than an unverified file.
        ("integrity_verified", Json::Bool(true)),
    ])
    .to_string_pretty())
}

fn cmd_list(args: &[String]) -> Result<String> {
    let repository = repo(args)?;
    let total_stored: u64 = repository.versions().iter().map(|v| v.stored_size).sum();
    let total_logical: u64 = repository.versions().iter().map(|v| v.size).sum();
    let versions: Vec<Json> = repository
        .versions()
        .iter()
        .map(|v| {
            Json::obj(vec![
                ("id", Json::str(sha256::to_hex(&v.id))),
                ("name", Json::str(&v.name)),
                (
                    "base_id",
                    match &v.base_id {
                        Some(b) => Json::str(sha256::to_hex(b)),
                        None => Json::Null,
                    },
                ),
                ("size", Json::uint(v.size)),
                ("stored_size", Json::uint(v.stored_size)),
                ("chain_depth", Json::uint(v.chain_depth)),
                ("content_sha256", Json::str(sha256::to_hex(&v.content_sha256))),
            ])
        })
        .collect();
    Ok(Json::obj(vec![
        ("ok", Json::Bool(true)),
        ("count", Json::uint(versions.len() as u64)),
        ("total_logical_size", Json::uint(total_logical)),
        ("total_stored_size", Json::uint(total_stored)),
        ("versions", Json::Array(versions)),
    ])
    .to_string_pretty())
}

fn cmd_inspect(args: &[String]) -> Result<String> {
    let repository = repo(args)?;
    let record = repository.find(required(args, "--version")?)?;
    let path = repository.container_path(&record.id);
    let (header, index) = store::inspect(&path)?;
    let kinds: Vec<Json> = store::summarise_kinds(&index)
        .into_iter()
        .map(|(k, n)| Json::obj(vec![("kind", Json::str(k)), ("blocks", Json::uint(n))]))
        .collect();
    let blocks: Vec<Json> = index
        .iter()
        .enumerate()
        .map(|(i, e)| {
            Json::obj(vec![
                ("index", Json::uint(i as u64)),
                ("kind", Json::str(e.kind.as_str())),
                ("logical_len", Json::uint(e.logical_len as u64)),
                ("payload_len", Json::uint(e.payload_len as u64)),
                ("changed_bits", Json::uint(e.changed_bits as u64)),
                ("changed_bytes", Json::uint(e.changed_bytes as u64)),
            ])
        })
        .collect();
    Ok(Json::obj(vec![
        ("ok", Json::Bool(true)),
        ("name", Json::str(&record.name)),
        ("container", Json::str(path.display().to_string())),
        ("format_version", Json::uint(header.format_version as u64)),
        ("is_root", Json::Bool(header.is_root())),
        ("original_size", Json::uint(header.original_size)),
        ("base_size", Json::uint(header.base_size)),
        ("block_size", Json::uint(header.block_size)),
        ("block_count", Json::uint(header.block_count)),
        ("content_sha256", Json::str(sha256::to_hex(&header.content_sha256))),
        ("container_integrity_verified", Json::Bool(true)),
        ("representation_summary", Json::Array(kinds)),
        ("blocks", Json::Array(blocks)),
    ])
    .to_string_pretty())
}

/// Verifies containers end to end: every version is reconstructed to a temporary
/// file and its hash checked. This is the only honest form of "verify" — reading
/// the header proves nothing about whether the bytes can be recovered.
fn cmd_verify(args: &[String]) -> Result<String> {
    let repository = repo(args)?;
    let targets: Vec<_> = match flag(args, "--version") {
        Some(v) => vec![repository.find(v)?.clone()],
        None => repository.versions().to_vec(),
    };

    let tmp_dir = std::env::temp_dir().join(format!("ccp-verify-{}", std::process::id()));
    std::fs::create_dir_all(&tmp_dir)?;

    let mut results = Vec::new();
    let mut failures = 0u64;
    for v in &targets {
        let out = tmp_dir.join(format!("{}.bin", sha256::to_hex(&v.id)));
        let outcome = repository.reconstruct(&v.name, &out);
        let _ = std::fs::remove_file(&out);
        let (ok, message) = match outcome {
            Ok(_) => (true, String::new()),
            Err(e) => {
                failures += 1;
                (false, e.to_string())
            }
        };
        results.push(Json::obj(vec![
            ("name", Json::str(&v.name)),
            ("verified", Json::Bool(ok)),
            ("message", Json::str(message)),
        ]));
    }
    let _ = std::fs::remove_dir_all(&tmp_dir);

    if failures > 0 {
        return Err(CcpError::Integrity(format!(
            "{failures} of {} versions failed verification",
            targets.len()
        )));
    }
    Ok(Json::obj(vec![
        ("ok", Json::Bool(true)),
        ("verified", Json::uint(targets.len() as u64)),
        ("results", Json::Array(results)),
    ])
    .to_string_pretty())
}
