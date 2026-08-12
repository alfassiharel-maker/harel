//! The machine-readable form of a trace.
//!
//! Written by hand because the shape is small and fixed, and because a
//! serialisation dependency would be the first third-party crate in the
//! workspace — a decision that needs the §30 justification, not a convenience
//! argument. Output is deterministic: fields appear in a fixed order, so two
//! runs of one program produce byte-identical JSON.

use crate::event::{Emitted, TraceEventKind};
use crate::trace::Trace;
use lml_types::Value;

/// Render a trace as JSON.
#[must_use]
pub fn to_json(trace: &Trace) -> String {
    let mut out = String::from("{\"trace_version\":1,\"events\":[");
    for (index, event) in trace.events().iter().enumerate() {
        if index > 0 {
            out.push(',');
        }
        out.push_str(&format!(
            "{{\"seq\":{},\"kind\":{}",
            event.seq,
            string(event.kind.name())
        ));
        out.push_str(&fields(&event.kind));
        out.push('}');
    }
    out.push_str("]}");
    out
}

fn fields(kind: &TraceEventKind) -> String {
    match kind {
        TraceEventKind::ExecutionStarted {
            language_version,
            ir_version,
            program_digest,
        } => {
            format!(
                ",\"language_version\":{},\"ir_version\":{ir_version},\"program_digest\":{}",
                string(language_version),
                string(program_digest)
            )
        }
        TraceEventKind::FactDeclared { name, value } => {
            format!(",\"name\":{},\"value\":{}", string(name), json_value(value))
        }
        TraceEventKind::RoundStarted { round } => format!(",\"round\":{round}"),
        TraceEventKind::RuleNotEvaluable { rule, missing } => {
            format!(
                ",\"rule\":{},\"missing\":{}",
                string(rule),
                string_array(missing)
            )
        }
        TraceEventKind::ConditionEvaluated { rule, result } => {
            format!(",\"rule\":{},\"result\":{result}", string(rule))
        }
        TraceEventKind::RuleActivated { rule, reads } => {
            format!(
                ",\"rule\":{},\"reads\":{}",
                string(rule),
                string_array(reads)
            )
        }
        TraceEventKind::FactDerived { name, value, rule }
        | TraceEventKind::DerivationRedundant { name, value, rule } => format!(
            ",\"name\":{},\"value\":{},\"rule\":{}",
            string(name),
            json_value(value),
            string(rule)
        ),
        TraceEventKind::RoundFinished { round, fired_any } => {
            format!(",\"round\":{round},\"fired_any\":{fired_any}")
        }
        TraceEventKind::OutputEmitted { name, value } => {
            let value = match value {
                Emitted::Known(value) => json_value(value),
                // `null` is the JSON of *unknown*, which is exactly what SQL
                // `NULL` means too (`docs/04_TYPE_AND_DATA_MODEL.md` §9).
                Emitted::Unknown => "null".to_owned(),
            };
            format!(",\"name\":{},\"value\":{value}", string(name))
        }
        TraceEventKind::ExecutionFinished {
            rounds,
            facts_total,
            rules_fired,
        } => {
            format!(
                ",\"rounds\":{rounds},\"facts_total\":{facts_total},\"rules_fired\":{rules_fired}"
            )
        }
        TraceEventKind::ErrorRaised { code, message } => {
            format!(",\"code\":{},\"message\":{}", string(code), string(message))
        }
    }
}

/// A value, tagged with its type.
///
/// Tagged rather than bare because JSON cannot tell `1` from `1.0`, and the
/// language takes that distinction seriously enough to refuse to compile
/// `1 + 1.0`. A consumer that reads a trace must not lose it.
fn json_value(value: &Value) -> String {
    match value {
        Value::Int(number) => format!("{{\"type\":\"Int\",\"value\":{number}}}"),
        Value::Float(number) => {
            // Always finite, so this is always valid JSON.
            format!("{{\"type\":\"Float\",\"value\":{number:?}}}")
        }
        Value::Bool(boolean) => format!("{{\"type\":\"Bool\",\"value\":{boolean}}}"),
        Value::Str(text) => format!("{{\"type\":\"String\",\"value\":{}}}", string(text)),
    }
}

fn string_array(items: &[String]) -> String {
    let rendered: Vec<String> = items.iter().map(|item| string(item)).collect();
    format!("[{}]", rendered.join(","))
}

/// A JSON string literal, escaped per RFC 8259.
fn string(text: &str) -> String {
    let mut out = String::with_capacity(text.len() + 2);
    out.push('"');
    for ch in text.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            control if control < ' ' => out.push_str(&format!("\\u{:04x}", control as u32)),
            other => out.push(other),
        }
    }
    out.push('"');
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn control_characters_are_escaped() {
        assert_eq!(string("a\u{1}b"), "\"a\\u0001b\"");
        assert_eq!(string("tab\there"), "\"tab\\there\"");
    }

    #[test]
    fn values_carry_their_type() {
        assert_eq!(json_value(&Value::Int(1)), "{\"type\":\"Int\",\"value\":1}");
        assert_eq!(
            json_value(&Value::Float(1.0)),
            "{\"type\":\"Float\",\"value\":1.0}"
        );
    }

    #[test]
    fn an_empty_trace_is_still_valid_json() {
        assert_eq!(
            to_json(&Trace::new()),
            "{\"trace_version\":1,\"events\":[]}"
        );
    }
}
