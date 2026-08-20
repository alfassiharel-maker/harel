//! The CCP container format, version 1.
//!
//! `../docs/format-v1.md` is the specification; this module is its
//! implementation and the tests below assert the constants match. Nothing else
//! in the codebase may read or write these bytes directly.
//!
//! Every reader rejects an unknown `format_version` rather than attempting a
//! best-effort parse. Stored data outlives code, so a future version must be a
//! deliberate migration, never an accident of a tolerant parser.

use crate::error::{CcpError, Result};

pub const MAGIC: [u8; 8] = *b"CCPFMT\0\0";
pub const FOOTER_MAGIC: [u8; 8] = *b"CCPEND\0\0";
pub const FORMAT_VERSION: u16 = 1;
pub const HEADER_SIZE: u16 = 128;
pub const INDEX_ENTRY_SIZE: u64 = 32;
pub const FOOTER_SIZE: u64 = 40;

/// How one block is represented on disk.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BlockKind {
    /// The target block, verbatim.
    Full = 0,
    /// `base XOR target`, verbatim. Same size as the block; chosen only when it
    /// is genuinely the cheapest option, which in practice means never for a
    /// dense change (Full ties and is simpler) — kept because it is the natural
    /// representation and the cost model must be free to pick it.
    DeltaRaw = 1,
    /// Sparse list of differing bytes. See the payload encoding in the spec.
    DeltaSparse = 2,
    /// The block is unchanged from the base; no payload at all.
    Identical = 3,
}

impl BlockKind {
    pub fn from_u8(v: u8) -> Result<Self> {
        match v {
            0 => Ok(BlockKind::Full),
            1 => Ok(BlockKind::DeltaRaw),
            2 => Ok(BlockKind::DeltaSparse),
            3 => Ok(BlockKind::Identical),
            other => Err(CcpError::Format(format!("unknown block kind {other}"))),
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            BlockKind::Full => "FULL",
            BlockKind::DeltaRaw => "DELTA_RAW",
            BlockKind::DeltaSparse => "DELTA_SPARSE",
            BlockKind::Identical => "IDENTICAL",
        }
    }

    pub fn from_str_name(s: &str) -> Result<Self> {
        match s {
            "FULL" => Ok(BlockKind::Full),
            "DELTA_RAW" => Ok(BlockKind::DeltaRaw),
            "DELTA_SPARSE" => Ok(BlockKind::DeltaSparse),
            "IDENTICAL" => Ok(BlockKind::Identical),
            other => Err(CcpError::Strategy(format!(
                "strategy engine returned unknown representation {other:?}"
            ))),
        }
    }

    /// True when reconstruction of this block needs the corresponding base block.
    pub fn needs_base(self) -> bool {
        !matches!(self, BlockKind::Full)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct IndexEntry {
    pub kind: BlockKind,
    pub payload_len: u32,
    pub payload_off: u64,
    pub logical_len: u32,
    pub changed_bits: u32,
    pub changed_bytes: u32,
}

impl IndexEntry {
    pub fn encode(&self) -> [u8; 32] {
        let mut b = [0u8; 32];
        b[0] = self.kind as u8;
        b[4..8].copy_from_slice(&self.payload_len.to_le_bytes());
        b[8..16].copy_from_slice(&self.payload_off.to_le_bytes());
        b[16..20].copy_from_slice(&self.logical_len.to_le_bytes());
        b[20..24].copy_from_slice(&self.changed_bits.to_le_bytes());
        b[24..28].copy_from_slice(&self.changed_bytes.to_le_bytes());
        b
    }

    pub fn decode(b: &[u8]) -> Result<Self> {
        if b.len() < 32 {
            return Err(CcpError::Format("truncated block index entry".into()));
        }
        Ok(IndexEntry {
            kind: BlockKind::from_u8(b[0])?,
            payload_len: u32::from_le_bytes(b[4..8].try_into().unwrap()),
            payload_off: u64::from_le_bytes(b[8..16].try_into().unwrap()),
            logical_len: u32::from_le_bytes(b[16..20].try_into().unwrap()),
            changed_bits: u32::from_le_bytes(b[20..24].try_into().unwrap()),
            changed_bytes: u32::from_le_bytes(b[24..28].try_into().unwrap()),
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Header {
    pub format_version: u16,
    pub block_size: u64,
    pub original_size: u64,
    pub block_count: u64,
    pub base_size: u64,
    pub version_id: [u8; 16],
    /// All-zero means this version is a reconstruction root.
    pub base_id: [u8; 16],
    pub content_sha256: [u8; 32],
    pub index_offset: u64,
    pub payload_offset: u64,
}

impl Header {
    pub fn is_root(&self) -> bool {
        self.base_id == [0u8; 16]
    }

    pub fn encode(&self) -> [u8; 128] {
        let mut b = [0u8; 128];
        b[0..8].copy_from_slice(&MAGIC);
        b[8..10].copy_from_slice(&self.format_version.to_le_bytes());
        b[10..12].copy_from_slice(&HEADER_SIZE.to_le_bytes());
        // 12..16 flags, reserved zero
        b[16..24].copy_from_slice(&self.block_size.to_le_bytes());
        b[24..32].copy_from_slice(&self.original_size.to_le_bytes());
        b[32..40].copy_from_slice(&self.block_count.to_le_bytes());
        b[40..48].copy_from_slice(&self.base_size.to_le_bytes());
        b[48..64].copy_from_slice(&self.version_id);
        b[64..80].copy_from_slice(&self.base_id);
        b[80..112].copy_from_slice(&self.content_sha256);
        b[112..120].copy_from_slice(&self.index_offset.to_le_bytes());
        b[120..128].copy_from_slice(&self.payload_offset.to_le_bytes());
        b
    }

    pub fn decode(b: &[u8]) -> Result<Self> {
        if b.len() < HEADER_SIZE as usize {
            return Err(CcpError::Format("truncated header".into()));
        }
        if b[0..8] != MAGIC {
            return Err(CcpError::Format("not a CCP container (bad magic)".into()));
        }
        let format_version = u16::from_le_bytes(b[8..10].try_into().unwrap());
        if format_version != FORMAT_VERSION {
            return Err(CcpError::UnsupportedVersion(format_version));
        }
        let header_size = u16::from_le_bytes(b[10..12].try_into().unwrap());
        if header_size != HEADER_SIZE {
            return Err(CcpError::Format(format!(
                "header_size {header_size} does not match version 1's {HEADER_SIZE}"
            )));
        }
        let flags = u32::from_le_bytes(b[12..16].try_into().unwrap());
        if flags != 0 {
            // An unknown flag may change the meaning of the payloads, so refusing
            // is the only safe reading.
            return Err(CcpError::Format(format!("unknown flags 0x{flags:08x}")));
        }
        let header = Header {
            format_version,
            block_size: u64::from_le_bytes(b[16..24].try_into().unwrap()),
            original_size: u64::from_le_bytes(b[24..32].try_into().unwrap()),
            block_count: u64::from_le_bytes(b[32..40].try_into().unwrap()),
            base_size: u64::from_le_bytes(b[40..48].try_into().unwrap()),
            version_id: b[48..64].try_into().unwrap(),
            base_id: b[64..80].try_into().unwrap(),
            content_sha256: b[80..112].try_into().unwrap(),
            index_offset: u64::from_le_bytes(b[112..120].try_into().unwrap()),
            payload_offset: u64::from_le_bytes(b[120..128].try_into().unwrap()),
        };
        if header.block_size == 0 {
            return Err(CcpError::Format("block_size must be non-zero".into()));
        }
        if header.index_offset != HEADER_SIZE as u64 {
            return Err(CcpError::Format("index_offset must follow the header in version 1".into()));
        }
        let expected_payload = HEADER_SIZE as u64 + header.block_count * INDEX_ENTRY_SIZE;
        if header.payload_offset != expected_payload {
            return Err(CcpError::Format(format!(
                "payload_offset {} does not match header + index ({expected_payload})",
                header.payload_offset
            )));
        }
        Ok(header)
    }
}

/// Encodes the differing bytes of `delta` as the `DELTA_SPARSE` payload.
///
/// `delta` is the XOR of the base and target blocks, so a non-zero byte is
/// exactly a changed byte. Returns `None` when the encoding would not fit in a
/// `u32` payload length, which the caller treats as "not a candidate".
pub fn encode_sparse(delta: &[u8]) -> Option<Vec<u8>> {
    let count = delta.iter().filter(|&&b| b != 0).count();
    let size = 4usize.checked_add(count.checked_mul(5)?)?;
    if size > u32::MAX as usize {
        return None;
    }
    let mut out = Vec::with_capacity(size);
    out.extend_from_slice(&(count as u32).to_le_bytes());
    for (offset, &value) in delta.iter().enumerate() {
        if value != 0 {
            out.extend_from_slice(&(offset as u32).to_le_bytes());
            out.push(value);
        }
    }
    debug_assert_eq!(out.len(), size);
    Some(out)
}

/// Cost of the sparse encoding without building it. Used when measuring
/// candidates, so the cost model never pays to materialise a representation it
/// might not choose.
pub fn sparse_cost(changed_bytes: u64) -> u64 {
    4 + changed_bytes * 5
}

/// Applies a `DELTA_SPARSE` payload onto `block`, which must already hold the
/// base block's bytes.
pub fn apply_sparse(payload: &[u8], block: &mut [u8]) -> Result<()> {
    if payload.len() < 4 {
        return Err(CcpError::Format("sparse payload shorter than its header".into()));
    }
    let count = u32::from_le_bytes(payload[0..4].try_into().unwrap()) as usize;
    let expected = 4 + count * 5;
    if payload.len() != expected {
        return Err(CcpError::Format(format!(
            "sparse payload length {} does not match its count {count} (expected {expected})",
            payload.len()
        )));
    }
    for i in 0..count {
        let at = 4 + i * 5;
        let offset = u32::from_le_bytes(payload[at..at + 4].try_into().unwrap()) as usize;
        let value = payload[at + 4];
        // A corrupt or hostile container must not write outside the block.
        let slot = block
            .get_mut(offset)
            .ok_or_else(|| CcpError::Format(format!("sparse entry offset {offset} outside block")))?;
        *slot ^= value;
    }
    Ok(())
}

pub fn encode_footer(container_sha256: &[u8; 32]) -> [u8; 40] {
    let mut b = [0u8; 40];
    b[0..8].copy_from_slice(&FOOTER_MAGIC);
    b[8..40].copy_from_slice(container_sha256);
    b
}

pub fn decode_footer(b: &[u8]) -> Result<[u8; 32]> {
    if b.len() < FOOTER_SIZE as usize {
        return Err(CcpError::Format("truncated footer".into()));
    }
    if b[0..8] != FOOTER_MAGIC {
        return Err(CcpError::Format("bad footer magic; container is truncated".into()));
    }
    Ok(b[8..40].try_into().unwrap())
}

#[cfg(test)]
mod tests {
    use super::*;

    // The specification states these sizes; a change here silently invalidates
    // every stored container, so they are asserted rather than trusted.
    #[test]
    fn layout_constants_match_the_specification() {
        assert_eq!(HEADER_SIZE, 128);
        assert_eq!(INDEX_ENTRY_SIZE, 32);
        assert_eq!(FOOTER_SIZE, 40);
        assert_eq!(&MAGIC, b"CCPFMT\0\0");
        assert_eq!(&FOOTER_MAGIC, b"CCPEND\0\0");
    }

    fn sample_header() -> Header {
        Header {
            format_version: FORMAT_VERSION,
            block_size: 4096,
            original_size: 10_000,
            block_count: 3,
            base_size: 9_000,
            version_id: [7u8; 16],
            base_id: [3u8; 16],
            content_sha256: [9u8; 32],
            index_offset: HEADER_SIZE as u64,
            payload_offset: HEADER_SIZE as u64 + 3 * INDEX_ENTRY_SIZE,
        }
    }

    #[test]
    fn header_roundtrip() {
        let h = sample_header();
        assert_eq!(Header::decode(&h.encode()).unwrap(), h);
    }

    #[test]
    fn header_rejects_a_future_format_version() {
        let mut bytes = sample_header().encode();
        bytes[8..10].copy_from_slice(&2u16.to_le_bytes());
        match Header::decode(&bytes) {
            Err(CcpError::UnsupportedVersion(2)) => {}
            other => panic!("must refuse an unknown format version, got {other:?}"),
        }
    }

    #[test]
    fn header_rejects_unknown_flags_and_bad_magic() {
        let mut bytes = sample_header().encode();
        bytes[12..16].copy_from_slice(&1u32.to_le_bytes());
        assert!(Header::decode(&bytes).is_err(), "unknown flags must be refused");

        let mut bytes = sample_header().encode();
        bytes[0] = b'X';
        assert!(Header::decode(&bytes).is_err(), "bad magic must be refused");
    }

    #[test]
    fn header_rejects_inconsistent_offsets() {
        let mut h = sample_header();
        h.payload_offset += 1;
        assert!(Header::decode(&h.encode()).is_err(), "payload offset must be checked");
    }

    #[test]
    fn index_entry_roundtrip() {
        let e = IndexEntry {
            kind: BlockKind::DeltaSparse,
            payload_len: 54,
            payload_off: 4096,
            logical_len: 4096,
            changed_bits: 17,
            changed_bytes: 10,
        };
        assert_eq!(IndexEntry::decode(&e.encode()).unwrap(), e);
    }

    #[test]
    fn sparse_roundtrip_reconstructs_the_target() {
        let base = vec![0xAAu8; 300];
        let mut target = base.clone();
        target[0] ^= 0x01;
        target[7] ^= 0xF0;
        target[299] ^= 0xFF;

        let delta: Vec<u8> = base.iter().zip(&target).map(|(a, b)| a ^ b).collect();
        let payload = encode_sparse(&delta).unwrap();
        assert_eq!(payload.len() as u64, sparse_cost(3), "cost formula must match the encoder");

        let mut rebuilt = base.clone();
        apply_sparse(&payload, &mut rebuilt).unwrap();
        assert_eq!(rebuilt, target, "sparse delta must reconstruct exactly");
    }

    #[test]
    fn sparse_of_an_unchanged_block_is_just_its_header() {
        let payload = encode_sparse(&[0u8; 4096]).unwrap();
        assert_eq!(payload, vec![0, 0, 0, 0]);
        assert_eq!(sparse_cost(0), 4);
    }

    #[test]
    fn apply_sparse_rejects_a_corrupt_payload() {
        // Offset beyond the block: a hostile container must not write out of bounds.
        let mut payload = vec![1u8, 0, 0, 0];
        payload.extend_from_slice(&999u32.to_le_bytes());
        payload.push(0xFF);
        let mut block = vec![0u8; 10];
        assert!(apply_sparse(&payload, &mut block).is_err());

        // Count that disagrees with the payload length.
        let mut payload = vec![5u8, 0, 0, 0];
        payload.extend_from_slice(&0u32.to_le_bytes());
        payload.push(1);
        assert!(apply_sparse(&payload, &mut vec![0u8; 10]).is_err());
    }

    #[test]
    fn footer_roundtrip_and_rejection() {
        let digest = [5u8; 32];
        assert_eq!(decode_footer(&encode_footer(&digest)).unwrap(), digest);
        let mut bad = encode_footer(&digest);
        bad[0] = b'Z';
        assert!(decode_footer(&bad).is_err());
    }

    #[test]
    fn block_kind_names_roundtrip() {
        for k in [BlockKind::Full, BlockKind::DeltaRaw, BlockKind::DeltaSparse, BlockKind::Identical] {
            assert_eq!(BlockKind::from_str_name(k.as_str()).unwrap(), k);
            assert_eq!(BlockKind::from_u8(k as u8).unwrap(), k);
        }
        assert!(BlockKind::from_str_name("NONSENSE").is_err());
        assert!(BlockKind::from_u8(9).is_err());
    }
}
