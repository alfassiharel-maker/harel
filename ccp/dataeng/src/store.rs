//! The repository: version graph, write path, read path.
//!
//! Memory discipline is the load-bearing property here. Every operation streams
//! in blocks and never holds more than a few blocks plus the block index, so peak
//! memory is a function of `block_size` and block *count*, not of artifact size.
//! A 100 GB artifact is processed with megabytes of RAM.
//!
//! Layout on disk:
//! ```text
//! <repo>/manifest.json          the version graph
//! <repo>/objects/<id>.ccp       one container per version
//! ```

use std::collections::BTreeMap;
use std::fs::{self, File};
use std::io::{BufReader, BufWriter, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

use crate::bitexec;
use crate::error::{CcpError, Result};
use crate::format::{self, BlockKind, Header, IndexEntry};
use crate::json::{self, Json};
use crate::sha256::{self, Sha256};
use crate::strategy::{BlockMeasurement, StrategyEngine};

/// Default block size. 1 MiB is a compromise, and worth stating: large enough
/// that per-block index overhead (32 bytes) is negligible and sequential reads
/// stay efficient, small enough that a single changed region does not force a
/// large FULL block. It is not tuned against real workloads yet — that is a
/// measurement, not a guess to enshrine.
pub const DEFAULT_BLOCK_SIZE: u64 = 1024 * 1024;

#[derive(Debug, Clone)]
pub struct VersionRecord {
    pub id: [u8; 16],
    pub name: String,
    pub base_id: Option<[u8; 16]>,
    /// Size of the artifact this version reconstructs to.
    pub size: u64,
    pub content_sha256: [u8; 32],
    /// Bytes the container actually occupies — the number that matters for the
    /// storage claim.
    pub stored_size: u64,
    /// Number of delta hops from a root. 0 for a root.
    pub chain_depth: u64,
    pub block_size: u64,
    pub created_unix: u64,
}

impl VersionRecord {
    fn to_json(&self) -> Json {
        Json::obj(vec![
            ("id", Json::str(sha256::to_hex(&self.id))),
            ("name", Json::str(&self.name)),
            (
                "base_id",
                match &self.base_id {
                    Some(b) => Json::str(sha256::to_hex(b)),
                    None => Json::Null,
                },
            ),
            ("size", Json::uint(self.size)),
            ("content_sha256", Json::str(sha256::to_hex(&self.content_sha256))),
            ("stored_size", Json::uint(self.stored_size)),
            ("chain_depth", Json::uint(self.chain_depth)),
            ("block_size", Json::uint(self.block_size)),
            ("created_unix", Json::uint(self.created_unix)),
        ])
    }

    fn from_json(v: &Json) -> Result<Self> {
        let bad = |what: &str| CcpError::Format(format!("manifest entry missing {what}"));
        let id16 = |s: &str| -> Result<[u8; 16]> {
            let bytes = (0..16)
                .map(|i| u8::from_str_radix(&s[i * 2..i * 2 + 2], 16))
                .collect::<std::result::Result<Vec<u8>, _>>()
                .map_err(|_| CcpError::Format(format!("bad id {s:?} in manifest")))?;
            Ok(bytes.try_into().unwrap())
        };
        let id_str = v.get("id").and_then(|x| x.as_str()).ok_or_else(|| bad("id"))?;
        if id_str.len() != 32 {
            return Err(CcpError::Format(format!("id {id_str:?} is not 16 bytes of hex")));
        }
        let base_id = match v.get("base_id") {
            Some(Json::String(s)) => {
                if s.len() != 32 {
                    return Err(CcpError::Format(format!("base_id {s:?} is not 16 bytes of hex")));
                }
                Some(id16(s)?)
            }
            _ => None,
        };
        Ok(VersionRecord {
            id: id16(id_str)?,
            name: v.get("name").and_then(|x| x.as_str()).ok_or_else(|| bad("name"))?.to_string(),
            base_id,
            size: v.get("size").and_then(|x| x.as_u64()).ok_or_else(|| bad("size"))?,
            content_sha256: v
                .get("content_sha256")
                .and_then(|x| x.as_str())
                .and_then(sha256::from_hex)
                .ok_or_else(|| bad("content_sha256"))?,
            stored_size: v.get("stored_size").and_then(|x| x.as_u64()).ok_or_else(|| bad("stored_size"))?,
            chain_depth: v.get("chain_depth").and_then(|x| x.as_u64()).ok_or_else(|| bad("chain_depth"))?,
            block_size: v.get("block_size").and_then(|x| x.as_u64()).ok_or_else(|| bad("block_size"))?,
            created_unix: v.get("created_unix").and_then(|x| x.as_u64()).unwrap_or(0),
        })
    }
}

/// What one store operation did, including the measurements behind each choice.
/// Returned so the caller can explain the decision without re-reading anything —
/// "explainable storage decisions" is a product requirement, not a debug aid.
#[derive(Debug, Clone)]
pub struct StoreOutcome {
    pub record: VersionRecord,
    pub base_name: Option<String>,
    pub blocks: Vec<BlockOutcome>,
    pub changed_bits: u64,
    pub changed_bytes: u64,
    /// What N full copies would have cost for this one version.
    pub full_size: u64,
    pub bitexec_features: Vec<&'static str>,
}

#[derive(Debug, Clone)]
pub struct BlockOutcome {
    pub index: u64,
    pub kind: BlockKind,
    pub logical_len: u64,
    pub payload_len: u64,
    pub changed_bits: u64,
    pub changed_bytes: u64,
    pub reason: String,
}

pub struct Repository {
    root: PathBuf,
    versions: Vec<VersionRecord>,
}

impl Repository {
    pub fn open_or_create(root: impl Into<PathBuf>) -> Result<Self> {
        let root = root.into();
        fs::create_dir_all(root.join("objects"))?;
        let manifest = root.join("manifest.json");
        let versions = if manifest.exists() {
            let text = fs::read_to_string(&manifest)?;
            let parsed = json::parse(&text)
                .map_err(|e| CcpError::Format(format!("manifest is not valid JSON: {e}")))?;
            let list = parsed
                .get("versions")
                .and_then(|v| v.as_array())
                .ok_or_else(|| CcpError::Format("manifest has no \"versions\" array".into()))?;
            list.iter().map(VersionRecord::from_json).collect::<Result<Vec<_>>>()?
        } else {
            Vec::new()
        };
        Ok(Repository { root, versions })
    }

    pub fn versions(&self) -> &[VersionRecord] {
        &self.versions
    }

    pub fn container_path(&self, id: &[u8; 16]) -> PathBuf {
        self.root.join("objects").join(format!("{}.ccp", sha256::to_hex(id)))
    }

    pub fn find(&self, name_or_id: &str) -> Result<&VersionRecord> {
        // Name first: it is what a person types. Ids are accepted in full or as a
        // unique prefix, which is only ambiguous if two ids share it — and that is
        // reported rather than resolved arbitrarily.
        if let Some(v) = self.versions.iter().find(|v| v.name == name_or_id) {
            return Ok(v);
        }
        let matches: Vec<&VersionRecord> = self
            .versions
            .iter()
            .filter(|v| sha256::to_hex(&v.id).starts_with(name_or_id))
            .collect();
        match matches.len() {
            1 => Ok(matches[0]),
            0 => Err(CcpError::NotFound(format!("no version named or starting with {name_or_id:?}"))),
            n => Err(CcpError::Invalid(format!("{name_or_id:?} matches {n} versions; be more specific"))),
        }
    }

    fn by_id(&self, id: &[u8; 16]) -> Result<&VersionRecord> {
        self.versions
            .iter()
            .find(|v| &v.id == id)
            .ok_or_else(|| CcpError::NotFound(format!("version id {}", sha256::to_hex(id))))
    }

    fn save_manifest(&self) -> Result<()> {
        let doc = Json::obj(vec![
            ("format", Json::uint(1)),
            ("versions", Json::Array(self.versions.iter().map(|v| v.to_json()).collect())),
        ]);
        // Write to a temporary file and rename: a crash mid-write must not leave a
        // manifest that no longer lists containers which exist on disk.
        let tmp = self.root.join("manifest.json.tmp");
        let final_path = self.root.join("manifest.json");
        {
            let mut f = File::create(&tmp)?;
            f.write_all(doc.to_string_pretty().as_bytes())?;
            f.write_all(b"\n")?;
            f.sync_all()?;
        }
        fs::rename(&tmp, &final_path)?;
        Ok(())
    }

    /// The chain from a root down to `id`, root first. Every version must reach a
    /// root; a cycle or a missing base is a corrupt repository, not a retryable
    /// condition.
    pub fn chain(&self, id: &[u8; 16]) -> Result<Vec<VersionRecord>> {
        let mut chain = Vec::new();
        let mut seen = std::collections::BTreeSet::new();
        let mut cursor = *id;
        loop {
            if !seen.insert(cursor) {
                return Err(CcpError::Format(format!(
                    "version graph contains a cycle at {}",
                    sha256::to_hex(&cursor)
                )));
            }
            let v = self.by_id(&cursor)?.clone();
            let base = v.base_id;
            chain.push(v);
            match base {
                Some(b) => cursor = b,
                None => break,
            }
        }
        chain.reverse();
        Ok(chain)
    }
}

// ---------------------------------------------------------------------------
// Write path
// ---------------------------------------------------------------------------

/// Reads exactly `buf.len()` bytes unless EOF, returning how many were read.
/// `Read::read` is permitted to return short reads on a healthy file, so relying
/// on it directly would silently mis-block a large artifact.
fn read_block(reader: &mut impl Read, buf: &mut [u8]) -> Result<usize> {
    let mut filled = 0;
    while filled < buf.len() {
        match reader.read(&mut buf[filled..])? {
            0 => break,
            n => filled += n,
        }
    }
    Ok(filled)
}

fn now_unix() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// Version ids are derived, not random: SHA-256 over the content hash, the name
/// and the creation time, truncated to 16 bytes. No RNG dependency, and two
/// versions with identical content but different names still get distinct ids.
fn derive_id(content_sha256: &[u8; 32], name: &str, created: u64) -> [u8; 16] {
    let mut h = Sha256::new();
    h.update(content_sha256);
    h.update(name.as_bytes());
    h.update(&created.to_le_bytes());
    h.finalize()[..16].try_into().unwrap()
}

impl Repository {
    /// Ingests `target` as a new version, choosing FULL or a delta per block.
    ///
    /// Three passes over the input, each streaming: hash the target, measure every
    /// block against the base, then write the chosen representations. Measuring
    /// before deciding is what makes the decision real — the cost model sees the
    /// whole artifact's measurements before any byte is committed.
    pub fn store(
        &mut self,
        target: &Path,
        name: &str,
        base: Option<&str>,
        block_size: u64,
        strategy: &StrategyEngine,
    ) -> Result<StoreOutcome> {
        if block_size == 0 {
            return Err(CcpError::Invalid("block size must be greater than zero".into()));
        }
        if self.versions.iter().any(|v| v.name == name) {
            return Err(CcpError::Invalid(format!("a version named {name:?} already exists")));
        }
        if !target.is_file() {
            return Err(CcpError::NotFound(format!("target artifact {}", target.display())));
        }

        let target_size = fs::metadata(target)?.len();
        let content_sha256 = hash_file(target)?;

        // Resolve the base and materialise it. Reconstructing the base to a
        // temporary file costs disk but keeps memory flat and, critically, means
        // the base bytes we delta against are the *verified* reconstruction — the
        // same bytes any future read will produce.
        let base_record = match base {
            Some(spec) => Some(self.find(spec)?.clone()),
            None => None,
        };
        let base_temp = match &base_record {
            Some(rec) => {
                let tmp = self.root.join(format!("objects/.base-{}.tmp", sha256::to_hex(&rec.id)));
                self.reconstruct_to(&rec.id.clone(), &tmp)?;
                Some(tmp)
            }
            None => None,
        };

        let result = self.store_inner(
            target,
            target_size,
            &content_sha256,
            name,
            base_record.as_ref(),
            base_temp.as_deref(),
            block_size,
            strategy,
        );

        if let Some(tmp) = &base_temp {
            let _ = fs::remove_file(tmp);
        }
        result
    }

    #[allow(clippy::too_many_arguments)]
    fn store_inner(
        &mut self,
        target: &Path,
        target_size: u64,
        content_sha256: &[u8; 32],
        name: &str,
        base_record: Option<&VersionRecord>,
        base_temp: Option<&Path>,
        block_size: u64,
        strategy: &StrategyEngine,
    ) -> Result<StoreOutcome> {
        let base_size = base_record.map(|r| r.size).unwrap_or(0);
        let block_count = target_size.div_ceil(block_size).max(1);

        // ---- pass 1: measure every block, in C++ -----------------------
        let mut measurements = Vec::with_capacity(block_count as usize);
        {
            let mut tr = BufReader::new(File::open(target)?);
            let mut br = match base_temp {
                Some(p) => Some(BufReader::new(File::open(p)?)),
                None => None,
            };
            let mut tbuf = vec![0u8; block_size as usize];
            let mut bbuf = vec![0u8; block_size as usize];

            for index in 0..block_count {
                let tn = read_block(&mut tr, &mut tbuf)?;
                let bn = match br.as_mut() {
                    Some(r) => read_block(r, &mut bbuf)?,
                    None => 0,
                };
                // The unequal-length policy from the format spec, applied in one
                // place: a block is delta-eligible only when both sides supply the
                // same number of bytes. Nothing is padded or truncated.
                let delta_eligible = base_temp.is_some() && bn == tn && tn > 0;
                let stats = if delta_eligible {
                    bitexec::measure(&bbuf[..bn], &tbuf[..tn])
                } else {
                    Default::default()
                };
                measurements.push(BlockMeasurement {
                    index,
                    logical_len: tn as u64,
                    changed_bits: stats.changed_bits,
                    changed_bytes: stats.changed_bytes,
                    delta_eligible,
                });
            }
        }

        // ---- the decision, in Julia ------------------------------------
        let chain_depth = base_record.map(|r| r.chain_depth).unwrap_or(0);
        let decisions = strategy.decide(block_size, chain_depth, &measurements)?;

        // ---- pass 2: write the container -------------------------------
        let created = now_unix();
        let id = derive_id(content_sha256, name, created);
        let container = self.container_path(&id);
        let payload_offset = format::HEADER_SIZE as u64 + block_count * format::INDEX_ENTRY_SIZE;

        let mut index = Vec::with_capacity(block_count as usize);
        let mut outcomes = Vec::with_capacity(block_count as usize);
        let mut total_changed_bits = 0u64;
        let mut total_changed_bytes = 0u64;

        {
            let mut out = BufWriter::new(File::create(&container)?);
            // The header is written last (block_count and the digests are known
            // now, but writing it after the payloads would mean seeking anyway);
            // reserve the header and index region so payload offsets are final.
            out.write_all(&vec![0u8; payload_offset as usize])?;

            let mut tr = BufReader::new(File::open(target)?);
            let mut br = match base_temp {
                Some(p) => Some(BufReader::new(File::open(p)?)),
                None => None,
            };
            let mut tbuf = vec![0u8; block_size as usize];
            let mut bbuf = vec![0u8; block_size as usize];
            let mut dbuf = vec![0u8; block_size as usize];
            let mut payload_cursor = 0u64;

            for (m, d) in measurements.iter().zip(&decisions) {
                let tn = read_block(&mut tr, &mut tbuf)?;
                let bn = match br.as_mut() {
                    Some(r) => read_block(r, &mut bbuf)?,
                    None => 0,
                };
                debug_assert_eq!(tn as u64, m.logical_len, "pass 2 must see the same blocks as pass 1");

                let payload: &[u8] = match d.kind {
                    BlockKind::Full => &tbuf[..tn],
                    BlockKind::Identical => &[],
                    BlockKind::DeltaRaw => {
                        let n = bitexec::xor(&bbuf[..bn], &tbuf[..tn], &mut dbuf[..tn]);
                        debug_assert_eq!(n, tn);
                        &dbuf[..tn]
                    }
                    BlockKind::DeltaSparse => {
                        bitexec::xor(&bbuf[..bn], &tbuf[..tn], &mut dbuf[..tn]);
                        // Held only for this block, then dropped: the sparse
                        // payload is bounded by the block, not by the artifact.
                        let encoded = format::encode_sparse(&dbuf[..tn]).ok_or_else(|| {
                            CcpError::Invalid(format!("block {} too large to encode sparsely", m.index))
                        })?;
                        out.write_all(&encoded)?;
                        index.push(IndexEntry {
                            kind: d.kind,
                            payload_len: encoded.len() as u32,
                            payload_off: payload_cursor,
                            logical_len: tn as u32,
                            changed_bits: m.changed_bits.min(u32::MAX as u64) as u32,
                            changed_bytes: m.changed_bytes.min(u32::MAX as u64) as u32,
                        });
                        outcomes.push(BlockOutcome {
                            index: m.index,
                            kind: d.kind,
                            logical_len: m.logical_len,
                            payload_len: encoded.len() as u64,
                            changed_bits: m.changed_bits,
                            changed_bytes: m.changed_bytes,
                            reason: d.reason.clone(),
                        });
                        payload_cursor += encoded.len() as u64;
                        total_changed_bits += m.changed_bits;
                        total_changed_bytes += m.changed_bytes;
                        continue;
                    }
                };

                out.write_all(payload)?;
                index.push(IndexEntry {
                    kind: d.kind,
                    payload_len: payload.len() as u32,
                    payload_off: payload_cursor,
                    logical_len: tn as u32,
                    changed_bits: m.changed_bits.min(u32::MAX as u64) as u32,
                    changed_bytes: m.changed_bytes.min(u32::MAX as u64) as u32,
                });
                outcomes.push(BlockOutcome {
                    index: m.index,
                    kind: d.kind,
                    logical_len: m.logical_len,
                    payload_len: payload.len() as u64,
                    changed_bits: m.changed_bits,
                    changed_bytes: m.changed_bytes,
                    reason: d.reason.clone(),
                });
                payload_cursor += payload.len() as u64;
                total_changed_bits += m.changed_bits;
                total_changed_bytes += m.changed_bytes;
            }

            // Header and index, back at the front.
            let header = Header {
                format_version: format::FORMAT_VERSION,
                block_size,
                original_size: target_size,
                block_count,
                base_size,
                version_id: id,
                base_id: base_record.map(|r| r.id).unwrap_or([0u8; 16]),
                content_sha256: *content_sha256,
                index_offset: format::HEADER_SIZE as u64,
                payload_offset,
            };
            out.seek(SeekFrom::Start(0))?;
            out.write_all(&header.encode())?;
            for entry in &index {
                out.write_all(&entry.encode())?;
            }
            out.flush()?;
        }

        // The footer hashes the container's own bytes, so it can only be computed
        // once they are all on disk.
        let container_digest = hash_file(&container)?;
        {
            let mut f = File::options().append(true).open(&container)?;
            f.write_all(&format::encode_footer(&container_digest))?;
            f.sync_all()?;
        }

        let record = VersionRecord {
            id,
            name: name.to_string(),
            base_id: base_record.map(|r| r.id),
            size: target_size,
            content_sha256: *content_sha256,
            stored_size: fs::metadata(&container)?.len(),
            chain_depth: base_record.map(|r| r.chain_depth + 1).unwrap_or(0),
            block_size,
            created_unix: created,
        };
        self.versions.push(record.clone());
        self.save_manifest()?;

        Ok(StoreOutcome {
            record,
            base_name: base_record.map(|r| r.name.clone()),
            blocks: outcomes,
            changed_bits: total_changed_bits,
            changed_bytes: total_changed_bytes,
            full_size: target_size,
            bitexec_features: bitexec::feature_names(),
        })
    }
}

// ---------------------------------------------------------------------------
// Read path
// ---------------------------------------------------------------------------

pub fn hash_file(path: &Path) -> Result<[u8; 32]> {
    let mut f = BufReader::new(File::open(path)?);
    let mut h = Sha256::new();
    // 64 KiB at a time: large enough to amortise syscalls, small enough that peak
    // memory is unrelated to file size.
    let mut buf = vec![0u8; 64 * 1024];
    loop {
        let n = f.read(&mut buf)?;
        if n == 0 {
            break;
        }
        h.update(&buf[..n]);
    }
    Ok(h.finalize())
}

/// Hashes a container's bytes excluding its footer, and compares against the
/// digest the footer carries. Detects truncation and corruption independently of
/// whether the reconstructed content happens to hash correctly.
fn verify_container(path: &Path) -> Result<()> {
    let total = fs::metadata(path)?.len();
    if total < format::HEADER_SIZE as u64 + format::FOOTER_SIZE {
        return Err(CcpError::Format(format!("{} is too small to be a container", path.display())));
    }
    let body_len = total - format::FOOTER_SIZE;
    let mut f = BufReader::new(File::open(path)?);
    let mut h = Sha256::new();
    let mut buf = vec![0u8; 64 * 1024];
    let mut remaining = body_len;
    while remaining > 0 {
        let want = (buf.len() as u64).min(remaining) as usize;
        let n = read_block(&mut f, &mut buf[..want])?;
        if n == 0 {
            return Err(CcpError::Format("container ended early".into()));
        }
        h.update(&buf[..n]);
        remaining -= n as u64;
    }
    let computed = h.finalize();
    let mut footer = [0u8; format::FOOTER_SIZE as usize];
    read_block(&mut f, &mut footer)?;
    let stored = format::decode_footer(&footer)?;
    if computed != stored {
        return Err(CcpError::Integrity(format!(
            "container {} does not match its own footer digest (stored {}, computed {})",
            path.display(),
            sha256::to_hex(&stored),
            sha256::to_hex(&computed)
        )));
    }
    Ok(())
}

impl Repository {
    /// Reconstructs a version to `out`, verifying integrity before returning.
    pub fn reconstruct(&self, name_or_id: &str, out: &Path) -> Result<VersionRecord> {
        let record = self.find(name_or_id)?.clone();
        self.reconstruct_to(&record.id, out)?;
        Ok(record)
    }

    /// Walks the chain from the root, materialising each version in turn.
    ///
    /// Intermediate versions go to temporary files rather than memory, so peak
    /// memory stays at one block regardless of artifact size — at the cost of disk
    /// and of re-reading each hop. That trade is exactly why chain depth is priced
    /// by the cost model.
    fn reconstruct_to(&self, id: &[u8; 16], out: &Path) -> Result<()> {
        let chain = self.chain(id)?;
        if let Some(parent) = out.parent() {
            fs::create_dir_all(parent)?;
        }

        let mut current: Option<PathBuf> = None;
        let mut temps: Vec<PathBuf> = Vec::new();

        for (step, version) in chain.iter().enumerate() {
            let last = step + 1 == chain.len();
            let dest = if last {
                out.to_path_buf()
            } else {
                let t = out.with_extension(format!("ccp-stage{step}"));
                temps.push(t.clone());
                t
            };

            let result = apply_container(
                &self.container_path(&version.id),
                current.as_deref(),
                &dest,
                version,
            );
            if let Err(e) = result {
                for t in &temps {
                    let _ = fs::remove_file(t);
                }
                return Err(e);
            }
            current = Some(dest);
        }

        for t in &temps {
            let _ = fs::remove_file(t);
        }
        Ok(())
    }
}

/// Materialises one version from its container and (if it is a delta) its base.
fn apply_container(
    container: &Path,
    base: Option<&Path>,
    dest: &Path,
    record: &VersionRecord,
) -> Result<()> {
    verify_container(container)?;

    let mut f = BufReader::new(File::open(container)?);
    let mut header_bytes = [0u8; format::HEADER_SIZE as usize];
    read_block(&mut f, &mut header_bytes)?;
    let header = Header::decode(&header_bytes)?;

    if header.version_id != record.id {
        return Err(CcpError::Integrity(format!(
            "container {} holds version {} but the manifest expected {}",
            container.display(),
            sha256::to_hex(&header.version_id),
            sha256::to_hex(&record.id)
        )));
    }
    if header.is_root() != base.is_none() {
        return Err(CcpError::Format(format!(
            "container {} is {} but was given {} base",
            container.display(),
            if header.is_root() { "a root" } else { "a delta" },
            if base.is_some() { "a" } else { "no" }
        )));
    }

    let mut index = Vec::with_capacity(header.block_count as usize);
    let mut entry_bytes = [0u8; format::INDEX_ENTRY_SIZE as usize];
    for _ in 0..header.block_count {
        read_block(&mut f, &mut entry_bytes)?;
        index.push(IndexEntry::decode(&entry_bytes)?);
    }

    let mut base_reader = match base {
        Some(p) => Some(BufReader::new(File::open(p)?)),
        None => None,
    };
    let mut out = BufWriter::new(File::create(dest)?);
    let mut block = vec![0u8; header.block_size as usize];
    let mut payload = Vec::new();
    let mut written = 0u64;

    for (i, entry) in index.iter().enumerate() {
        let logical = entry.logical_len as usize;
        if logical > header.block_size as usize {
            return Err(CcpError::Format(format!(
                "block {i} claims {logical} logical bytes, exceeding block size {}",
                header.block_size
            )));
        }

        // Payloads are stored in index order, so this is a sequential read; the
        // offset is used to seek only because a corrupt index must not be
        // followed blindly.
        f.seek(SeekFrom::Start(header.payload_offset + entry.payload_off))?;
        payload.resize(entry.payload_len as usize, 0);
        if !payload.is_empty() {
            let n = read_block(&mut f, &mut payload)?;
            if n != payload.len() {
                return Err(CcpError::Format(format!("block {i} payload is truncated")));
            }
        }

        if entry.kind.needs_base() {
            let reader = base_reader
                .as_mut()
                .ok_or_else(|| CcpError::Format(format!("block {i} needs a base but none was supplied")))?;
            let bn = read_block(reader, &mut block[..logical])?;
            if bn != logical {
                return Err(CcpError::Format(format!(
                    "base supplied {bn} bytes for block {i}, which needs {logical}"
                )));
            }
        }

        match entry.kind {
            BlockKind::Full => {
                if payload.len() != logical {
                    return Err(CcpError::Format(format!(
                        "FULL block {i} stores {} bytes for {logical} logical bytes",
                        payload.len()
                    )));
                }
                block[..logical].copy_from_slice(&payload);
            }
            // The reconstruction invariant, executed in C++: target = base XOR delta.
            BlockKind::DeltaRaw => {
                if payload.len() != logical {
                    return Err(CcpError::Format(format!("DELTA_RAW block {i} has the wrong payload length")));
                }
                bitexec::xor_inplace(&mut block[..logical], &payload);
            }
            BlockKind::DeltaSparse => format::apply_sparse(&payload, &mut block[..logical])?,
            // Nothing to do: the base block already holds the right bytes.
            BlockKind::Identical => {}
        }

        out.write_all(&block[..logical])?;
        written += logical as u64;
    }

    out.flush()?;
    drop(out);

    if written != header.original_size {
        return Err(CcpError::Integrity(format!(
            "reconstructed {written} bytes but the container records {}",
            header.original_size
        )));
    }

    // The end of the guarantee: the bytes we just produced must hash to what was
    // recorded when they were ingested. A mismatch is fatal and the output is
    // removed — returning a file that failed verification would be the one
    // outcome this system must never have.
    let digest = hash_file(dest)?;
    if digest != header.content_sha256 {
        let _ = fs::remove_file(dest);
        return Err(CcpError::Integrity(format!(
            "reconstruction of {} hashes to {} but ingest recorded {}",
            record.name,
            sha256::to_hex(&digest),
            sha256::to_hex(&header.content_sha256)
        )));
    }
    Ok(())
}

/// Reads a container's header and index without reconstructing anything.
pub fn inspect(container: &Path) -> Result<(Header, Vec<IndexEntry>)> {
    verify_container(container)?;
    let mut f = BufReader::new(File::open(container)?);
    let mut header_bytes = [0u8; format::HEADER_SIZE as usize];
    read_block(&mut f, &mut header_bytes)?;
    let header = Header::decode(&header_bytes)?;
    let mut index = Vec::with_capacity(header.block_count as usize);
    let mut entry = [0u8; format::INDEX_ENTRY_SIZE as usize];
    for _ in 0..header.block_count {
        read_block(&mut f, &mut entry)?;
        index.push(IndexEntry::decode(&entry)?);
    }
    Ok((header, index))
}

/// Totals per representation, for reporting.
pub fn summarise_kinds(index: &[IndexEntry]) -> BTreeMap<&'static str, u64> {
    let mut counts = BTreeMap::new();
    for e in index {
        *counts.entry(e.kind.as_str()).or_insert(0) += 1;
    }
    counts
}
