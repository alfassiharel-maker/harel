//! Bridge to the Julia strategy engine.
//!
//! The division of labour this module enforces: Rust *measures*, Julia *decides*.
//! Rust sends per-block measurements and the encoding parameters (so Julia knows
//! what each representation would cost per byte), and Julia returns one decision
//! per block with its reasoning. No cost arithmetic happens on this side — if a
//! threshold or a formula appears in this file, the boundary has been violated.
//!
//! Julia is a hard dependency, not an optional accelerator. There is deliberately
//! no built-in fallback cost model: a second implementation of the policy would
//! drift from the real one and mask its absence, and a system that silently
//! decides differently depending on what is installed cannot be reasoned about.

use std::io::Write;
use std::process::{Command, Stdio};

use crate::error::{CcpError, Result};
use crate::format::BlockKind;
use crate::json::{self, Json};

/// What Rust measured about one block. Every field is an observation, never a cost.
#[derive(Debug, Clone, Copy)]
pub struct BlockMeasurement {
    pub index: u64,
    /// Bytes this block contributes to the reconstructed artifact.
    pub logical_len: u64,
    pub changed_bits: u64,
    pub changed_bytes: u64,
    /// False when there is no base block to compare against — a length change, or
    /// a root version. Julia must then answer FULL, and is told so explicitly
    /// rather than having to infer it.
    pub delta_eligible: bool,
}

#[derive(Debug, Clone)]
pub struct BlockDecision {
    pub index: u64,
    pub kind: BlockKind,
    pub reason: String,
}

pub struct StrategyEngine {
    julia: String,
    script: std::path::PathBuf,
}

impl StrategyEngine {
    pub fn new(julia: impl Into<String>, script: impl Into<std::path::PathBuf>) -> Self {
        StrategyEngine { julia: julia.into(), script: script.into() }
    }

    /// Locates the engine relative to this crate, so the CLI works from any cwd.
    pub fn discover() -> Result<Self> {
        let julia = std::env::var("CCP_JULIA").unwrap_or_else(|_| "julia".to_string());
        let script = match std::env::var("CCP_STRATEGY_SCRIPT") {
            Ok(p) => std::path::PathBuf::from(p),
            Err(_) => {
                let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
                    .parent()
                    .ok_or_else(|| CcpError::Strategy("cannot locate the ccp root".into()))?
                    .to_path_buf();
                root.join("strategy/bin/ccp_strategy.jl")
            }
        };
        if !script.exists() {
            return Err(CcpError::Strategy(format!(
                "strategy engine script not found at {}; set CCP_STRATEGY_SCRIPT",
                script.display()
            )));
        }
        Ok(StrategyEngine::new(julia, script))
    }

    /// Asks Julia to choose a representation for every block in one call.
    ///
    /// Batched deliberately: the decision for a block depends on chain depth and
    /// on parameters shared across the artifact, and a process launch per block
    /// would dominate the runtime for no benefit.
    pub fn decide(
        &self,
        block_size: u64,
        chain_depth: u64,
        measurements: &[BlockMeasurement],
    ) -> Result<Vec<BlockDecision>> {
        let request = self.build_request(block_size, chain_depth, measurements);

        let mut child = Command::new(&self.julia)
            .arg("--startup-file=no")
            .arg("--history-file=no")
            .arg("-q")
            .arg(&self.script)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|e| {
                CcpError::Strategy(format!(
                    "cannot launch the strategy engine ({} {}): {e}. \
                     Julia is required; there is no fallback cost model by design.",
                    self.julia,
                    self.script.display()
                ))
            })?;

        child
            .stdin
            .take()
            .ok_or_else(|| CcpError::Strategy("strategy engine stdin unavailable".into()))?
            .write_all(request.to_string_compact().as_bytes())
            .map_err(|e| CcpError::Strategy(format!("writing to the strategy engine failed: {e}")))?;

        let output = child
            .wait_with_output()
            .map_err(|e| CcpError::Strategy(format!("strategy engine did not complete: {e}")))?;

        if !output.status.success() {
            return Err(CcpError::Strategy(format!(
                "strategy engine exited with {}: {}",
                output.status,
                String::from_utf8_lossy(&output.stderr).trim()
            )));
        }

        let stdout = String::from_utf8_lossy(&output.stdout);
        self.parse_response(&stdout, measurements)
    }

    fn build_request(
        &self,
        block_size: u64,
        chain_depth: u64,
        measurements: &[BlockMeasurement],
    ) -> Json {
        let blocks: Vec<Json> = measurements
            .iter()
            .map(|m| {
                Json::obj(vec![
                    ("index", Json::uint(m.index)),
                    ("logical_len", Json::uint(m.logical_len)),
                    ("changed_bits", Json::uint(m.changed_bits)),
                    ("changed_bytes", Json::uint(m.changed_bytes)),
                    ("delta_eligible", Json::Bool(m.delta_eligible)),
                ])
            })
            .collect();

        Json::obj(vec![
            ("protocol", Json::uint(1)),
            ("block_size", Json::uint(block_size)),
            // Chain depth of the *base*, so Julia can price what accepting another
            // delta hop would cost on every future read of this version.
            ("chain_depth", Json::uint(chain_depth)),
            // The encoding parameters, not the costs. Julia does the arithmetic;
            // it just needs to know what the writer can actually emit.
            (
                "encodings",
                Json::obj(vec![
                    (
                        "sparse",
                        Json::obj(vec![
                            ("header_bytes", Json::uint(4)),
                            ("bytes_per_changed_byte", Json::uint(5)),
                        ]),
                    ),
                    ("raw", Json::obj(vec![("header_bytes", Json::uint(0))])),
                    ("identical", Json::obj(vec![("header_bytes", Json::uint(0))])),
                ]),
            ),
            ("index_entry_bytes", Json::uint(crate::format::INDEX_ENTRY_SIZE)),
            ("blocks", Json::Array(blocks)),
        ])
    }

    fn parse_response(
        &self,
        stdout: &str,
        measurements: &[BlockMeasurement],
    ) -> Result<Vec<BlockDecision>> {
        let parsed = json::parse(stdout.trim()).map_err(|e| {
            CcpError::Strategy(format!(
                "strategy engine returned unparseable output: {e}\n--- output ---\n{}",
                stdout.trim()
            ))
        })?;

        let items = parsed
            .get("decisions")
            .and_then(|d| d.as_array())
            .ok_or_else(|| CcpError::Strategy("response has no \"decisions\" array".into()))?;

        if items.len() != measurements.len() {
            return Err(CcpError::Strategy(format!(
                "strategy engine returned {} decisions for {} blocks",
                items.len(),
                measurements.len()
            )));
        }

        let mut decisions = Vec::with_capacity(items.len());
        for (item, m) in items.iter().zip(measurements) {
            let index = item
                .get("index")
                .and_then(|v| v.as_u64())
                .ok_or_else(|| CcpError::Strategy("decision without an index".into()))?;
            if index != m.index {
                return Err(CcpError::Strategy(format!(
                    "decision order does not match the request: expected block {}, got {index}",
                    m.index
                )));
            }
            let kind_name = item
                .get("kind")
                .and_then(|v| v.as_str())
                .ok_or_else(|| CcpError::Strategy(format!("decision for block {index} has no kind")))?;
            let kind = BlockKind::from_str_name(kind_name)?;

            // The engine must not hand back a representation the block cannot
            // support. Trusting it here would produce a container that cannot be
            // reconstructed, so this is checked rather than assumed.
            if !m.delta_eligible && kind != BlockKind::Full {
                return Err(CcpError::Strategy(format!(
                    "block {index} has no base block but the strategy engine chose {kind_name}"
                )));
            }
            if kind == BlockKind::Identical && m.changed_bytes != 0 {
                return Err(CcpError::Strategy(format!(
                    "block {index} differs in {} bytes but the strategy engine chose IDENTICAL",
                    m.changed_bytes
                )));
            }

            decisions.push(BlockDecision {
                index,
                kind,
                reason: item
                    .get("reason")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string(),
            });
        }
        Ok(decisions)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn engine() -> StrategyEngine {
        StrategyEngine::new("julia", "unused-in-these-tests")
    }

    fn measurement(index: u64, changed_bytes: u64, eligible: bool) -> BlockMeasurement {
        BlockMeasurement {
            index,
            logical_len: 4096,
            changed_bits: changed_bytes * 8,
            changed_bytes,
            delta_eligible: eligible,
        }
    }

    #[test]
    fn request_carries_measurements_not_costs() {
        let req = engine().build_request(4096, 2, &[measurement(0, 10, true)]);
        assert_eq!(req.get("chain_depth").unwrap().as_u64(), Some(2));
        let b = &req.get("blocks").unwrap().as_array().unwrap()[0];
        assert_eq!(b.get("changed_bytes").unwrap().as_u64(), Some(10));
        assert_eq!(b.get("delta_eligible").unwrap().as_bool(), Some(true));
        // No cost field may be sent: costing is Julia's job.
        assert!(b.get("sparse_cost").is_none());
        assert!(b.get("full_cost").is_none());
    }

    #[test]
    fn parses_a_well_formed_response() {
        let out = r#"{"decisions":[{"index":0,"kind":"DELTA_SPARSE","reason":"sparse wins"}]}"#;
        let d = engine().parse_response(out, &[measurement(0, 10, true)]).unwrap();
        assert_eq!(d[0].kind, BlockKind::DeltaSparse);
        assert_eq!(d[0].reason, "sparse wins");
    }

    #[test]
    fn rejects_a_delta_choice_for_an_ineligible_block() {
        // The most dangerous possible bad answer: it would produce a container
        // that cannot be reconstructed.
        let out = r#"{"decisions":[{"index":0,"kind":"DELTA_RAW","reason":"x"}]}"#;
        assert!(engine().parse_response(out, &[measurement(0, 10, false)]).is_err());
    }

    #[test]
    fn rejects_identical_for_a_changed_block() {
        let out = r#"{"decisions":[{"index":0,"kind":"IDENTICAL","reason":"x"}]}"#;
        assert!(engine().parse_response(out, &[measurement(0, 7, true)]).is_err());
    }

    #[test]
    fn rejects_wrong_count_wrong_order_and_garbage() {
        let e = engine();
        let two = [measurement(0, 1, true), measurement(1, 1, true)];
        assert!(e
            .parse_response(r#"{"decisions":[{"index":0,"kind":"FULL"}]}"#, &two)
            .is_err());
        assert!(e
            .parse_response(
                r#"{"decisions":[{"index":1,"kind":"FULL"},{"index":0,"kind":"FULL"}]}"#,
                &two
            )
            .is_err());
        assert!(e.parse_response("not json", &two[..1]).is_err());
        assert!(e.parse_response(r#"{"nope":[]}"#, &two[..1]).is_err());
    }
}
