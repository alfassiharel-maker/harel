//! Minimal JSON, used for two things only: the manifest on disk and the
//! request/response exchanged with the Julia strategy engine.
//!
//! In-tree rather than a crate for the same reason as the hash — the manifest is
//! stored data. The subset is deliberate: objects, arrays, strings, numbers,
//! booleans and null, which is everything the two consumers need. It rejects what
//! it does not understand instead of guessing, because a silently mis-parsed
//! manifest would mean losing track of stored versions.

use std::collections::BTreeMap;
use std::fmt::Write as _;

#[derive(Debug, Clone, PartialEq)]
pub enum Json {
    Null,
    Bool(bool),
    /// All numbers are f64, as in JSON itself. Integer accessors below check that
    /// a value is exactly representable rather than truncating silently — an
    /// offset or length that does not round-trip is a corrupt manifest, not a
    /// rounding matter.
    Number(f64),
    String(String),
    Array(Vec<Json>),
    /// BTreeMap rather than HashMap so serialisation is byte-stable: a manifest
    /// rewritten with unchanged content produces an identical file, which makes
    /// diffs and change detection meaningful.
    Object(BTreeMap<String, Json>),
}

impl Json {
    pub fn get(&self, key: &str) -> Option<&Json> {
        match self {
            Json::Object(m) => m.get(key),
            _ => None,
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            Json::String(s) => Some(s),
            _ => None,
        }
    }

    pub fn as_array(&self) -> Option<&[Json]> {
        match self {
            Json::Array(v) => Some(v),
            _ => None,
        }
    }

    pub fn as_f64(&self) -> Option<f64> {
        match self {
            Json::Number(n) => Some(*n),
            _ => None,
        }
    }

    pub fn as_u64(&self) -> Option<u64> {
        match self {
            Json::Number(n) => {
                if *n < 0.0 || n.fract() != 0.0 || *n > (2f64).powi(53) {
                    None
                } else {
                    Some(*n as u64)
                }
            }
            _ => None,
        }
    }

    pub fn as_bool(&self) -> Option<bool> {
        match self {
            Json::Bool(b) => Some(*b),
            _ => None,
        }
    }

    pub fn obj(pairs: Vec<(&str, Json)>) -> Json {
        Json::Object(pairs.into_iter().map(|(k, v)| (k.to_string(), v)).collect())
    }

    pub fn str(s: impl Into<String>) -> Json {
        Json::String(s.into())
    }

    pub fn num(n: impl Into<f64>) -> Json {
        Json::Number(n.into())
    }

    pub fn uint(n: u64) -> Json {
        Json::Number(n as f64)
    }

    pub fn to_string_pretty(&self) -> String {
        let mut s = String::new();
        self.write(&mut s, 0, true);
        s
    }

    pub fn to_string_compact(&self) -> String {
        let mut s = String::new();
        self.write(&mut s, 0, false);
        s
    }

    fn write(&self, out: &mut String, depth: usize, pretty: bool) {
        let (nl, pad, pad_in) = if pretty {
            ("\n", "  ".repeat(depth), "  ".repeat(depth + 1))
        } else {
            ("", String::new(), String::new())
        };
        match self {
            Json::Null => out.push_str("null"),
            Json::Bool(true) => out.push_str("true"),
            Json::Bool(false) => out.push_str("false"),
            Json::Number(n) => {
                // Integers are written without a decimal point so a manifest read
                // by a human (or by jq) shows 4096, not 4096.0.
                if n.fract() == 0.0 && n.abs() < (2f64).powi(53) {
                    let _ = write!(out, "{}", *n as i64);
                } else {
                    let _ = write!(out, "{n}");
                }
            }
            Json::String(s) => write_string(out, s),
            Json::Array(items) => {
                if items.is_empty() {
                    out.push_str("[]");
                    return;
                }
                out.push('[');
                for (i, item) in items.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    out.push_str(nl);
                    out.push_str(&pad_in);
                    item.write(out, depth + 1, pretty);
                }
                out.push_str(nl);
                out.push_str(&pad);
                out.push(']');
            }
            Json::Object(map) => {
                if map.is_empty() {
                    out.push_str("{}");
                    return;
                }
                out.push('{');
                for (i, (k, v)) in map.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    out.push_str(nl);
                    out.push_str(&pad_in);
                    write_string(out, k);
                    out.push(':');
                    if pretty {
                        out.push(' ');
                    }
                    v.write(out, depth + 1, pretty);
                }
                out.push_str(nl);
                out.push_str(&pad);
                out.push('}');
            }
        }
    }
}

fn write_string(out: &mut String, s: &str) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

pub fn parse(input: &str) -> Result<Json, String> {
    let mut p = Parser { bytes: input.as_bytes(), pos: 0 };
    p.skip_ws();
    let v = p.value()?;
    p.skip_ws();
    if p.pos != p.bytes.len() {
        return Err(format!("trailing input at byte {}", p.pos));
    }
    Ok(v)
}

struct Parser<'a> {
    bytes: &'a [u8],
    pos: usize,
}

impl<'a> Parser<'a> {
    fn skip_ws(&mut self) {
        while self.pos < self.bytes.len() && matches!(self.bytes[self.pos], b' ' | b'\t' | b'\n' | b'\r') {
            self.pos += 1;
        }
    }

    fn peek(&self) -> Option<u8> {
        self.bytes.get(self.pos).copied()
    }

    fn expect(&mut self, b: u8) -> Result<(), String> {
        if self.peek() == Some(b) {
            self.pos += 1;
            Ok(())
        } else {
            Err(format!("expected {:?} at byte {}", b as char, self.pos))
        }
    }

    fn literal(&mut self, word: &str) -> Result<(), String> {
        if self.bytes[self.pos..].starts_with(word.as_bytes()) {
            self.pos += word.len();
            Ok(())
        } else {
            Err(format!("expected {word:?} at byte {}", self.pos))
        }
    }

    fn value(&mut self) -> Result<Json, String> {
        self.skip_ws();
        match self.peek() {
            None => Err("unexpected end of input".to_string()),
            Some(b'{') => self.object(),
            Some(b'[') => self.array(),
            Some(b'"') => Ok(Json::String(self.string()?)),
            Some(b't') => {
                self.literal("true")?;
                Ok(Json::Bool(true))
            }
            Some(b'f') => {
                self.literal("false")?;
                Ok(Json::Bool(false))
            }
            Some(b'n') => {
                self.literal("null")?;
                Ok(Json::Null)
            }
            Some(_) => self.number(),
        }
    }

    fn object(&mut self) -> Result<Json, String> {
        self.expect(b'{')?;
        let mut map = BTreeMap::new();
        self.skip_ws();
        if self.peek() == Some(b'}') {
            self.pos += 1;
            return Ok(Json::Object(map));
        }
        loop {
            self.skip_ws();
            let key = self.string()?;
            self.skip_ws();
            self.expect(b':')?;
            let value = self.value()?;
            map.insert(key, value);
            self.skip_ws();
            match self.peek() {
                Some(b',') => self.pos += 1,
                Some(b'}') => {
                    self.pos += 1;
                    return Ok(Json::Object(map));
                }
                _ => return Err(format!("expected ',' or '}}' at byte {}", self.pos)),
            }
        }
    }

    fn array(&mut self) -> Result<Json, String> {
        self.expect(b'[')?;
        let mut items = Vec::new();
        self.skip_ws();
        if self.peek() == Some(b']') {
            self.pos += 1;
            return Ok(Json::Array(items));
        }
        loop {
            items.push(self.value()?);
            self.skip_ws();
            match self.peek() {
                Some(b',') => self.pos += 1,
                Some(b']') => {
                    self.pos += 1;
                    return Ok(Json::Array(items));
                }
                _ => return Err(format!("expected ',' or ']' at byte {}", self.pos)),
            }
        }
    }

    fn string(&mut self) -> Result<String, String> {
        self.expect(b'"')?;
        let mut s = String::new();
        loop {
            let b = self.peek().ok_or("unterminated string")?;
            self.pos += 1;
            match b {
                b'"' => return Ok(s),
                b'\\' => {
                    let e = self.peek().ok_or("unterminated escape")?;
                    self.pos += 1;
                    match e {
                        b'"' => s.push('"'),
                        b'\\' => s.push('\\'),
                        b'/' => s.push('/'),
                        b'n' => s.push('\n'),
                        b'r' => s.push('\r'),
                        b't' => s.push('\t'),
                        b'b' => s.push('\u{8}'),
                        b'f' => s.push('\u{c}'),
                        b'u' => {
                            let hex = self
                                .bytes
                                .get(self.pos..self.pos + 4)
                                .ok_or("truncated \\u escape")?;
                            let hex = std::str::from_utf8(hex).map_err(|_| "bad \\u escape")?;
                            let code = u32::from_str_radix(hex, 16).map_err(|_| "bad \\u escape")?;
                            self.pos += 4;
                            // Surrogate pairs are not produced by either producer
                            // (Rust or Julia) for the ASCII keys and hex digests
                            // exchanged here, so rejecting them is honest rather
                            // than lossy.
                            s.push(char::from_u32(code).ok_or("unsupported \\u escape")?);
                        }
                        other => return Err(format!("bad escape \\{}", other as char)),
                    }
                }
                b => {
                    // Multi-byte UTF-8 arrives as raw bytes; collect the whole
                    // sequence rather than pushing each byte as a char.
                    if b < 0x80 {
                        s.push(b as char);
                    } else {
                        let len = if b >= 0xF0 {
                            4
                        } else if b >= 0xE0 {
                            3
                        } else {
                            2
                        };
                        let start = self.pos - 1;
                        let seq = self
                            .bytes
                            .get(start..start + len)
                            .ok_or("truncated UTF-8 sequence")?;
                        s.push_str(std::str::from_utf8(seq).map_err(|_| "invalid UTF-8")?);
                        self.pos = start + len;
                    }
                }
            }
        }
    }

    fn number(&mut self) -> Result<Json, String> {
        let start = self.pos;
        if self.peek() == Some(b'-') || self.peek() == Some(b'+') {
            self.pos += 1;
        }
        while let Some(b) = self.peek() {
            if b.is_ascii_digit() || matches!(b, b'.' | b'e' | b'E' | b'+' | b'-') {
                self.pos += 1;
            } else {
                break;
            }
        }
        let text = std::str::from_utf8(&self.bytes[start..self.pos]).map_err(|_| "bad number")?;
        text.parse::<f64>()
            .map(Json::Number)
            .map_err(|_| format!("bad number {text:?} at byte {start}"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip_nested() {
        let v = Json::obj(vec![
            ("name", Json::str("v2")),
            ("blocks", Json::Array(vec![Json::uint(0), Json::uint(4096)])),
            ("ratio", Json::num(0.125)),
            ("root", Json::Bool(false)),
            ("base", Json::Null),
            ("nested", Json::obj(vec![("k", Json::str("a\"b\nc"))])),
        ]);
        for text in [v.to_string_compact(), v.to_string_pretty()] {
            assert_eq!(parse(&text).unwrap(), v, "roundtrip of {text}");
        }
    }

    #[test]
    fn integers_do_not_gain_a_decimal_point() {
        assert_eq!(Json::uint(4096).to_string_compact(), "4096");
        assert_eq!(Json::uint(0).to_string_compact(), "0");
    }

    #[test]
    fn as_u64_rejects_lossy_values() {
        assert_eq!(Json::num(1.5).as_u64(), None);
        assert_eq!(Json::num(-1.0).as_u64(), None);
        assert_eq!(Json::uint(4096).as_u64(), Some(4096));
    }

    #[test]
    fn rejects_malformed_input() {
        for bad in ["{", "[1,]", "{\"a\"}", "tru", "", "{} x", "\"unterminated"] {
            assert!(parse(bad).is_err(), "must reject {bad:?}");
        }
    }

    #[test]
    fn parses_julia_style_output() {
        // Julia's JSON writer emits compact objects with no spaces; this is the
        // exact shape the strategy bridge receives.
        let text = r#"{"decisions":[{"index":0,"kind":"DELTA_SPARSE","reason":"sparse"}],"engine":"julia"}"#;
        let v = parse(text).unwrap();
        let d = v.get("decisions").unwrap().as_array().unwrap();
        assert_eq!(d[0].get("kind").unwrap().as_str(), Some("DELTA_SPARSE"));
    }
}
