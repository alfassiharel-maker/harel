//! The machine-readable form of a trace.
//!
//! **Every value is tagged with its state.** JSON `null` is never used for
//! either `Unknown` or `Null`: owner instruction §9 requires the distinction to
//! survive serialisation, and a bare `null` would collapse exactly the two
//! states OD-2 separates. This is the fix for the violation the design audit
//! recorded.
//!
//! ```json
//! {"state":"Unknown"}
//! {"state":"Null"}
//! {"state":"Known","type":"Int","value":31}
//! ```
//!
//! The `state` tag comes first in every value object, so a consumer can branch
//! on it without buffering. Written by hand because the shape is small and
//! fixed, and because a serialisation dependency would be the first third-party
//! crate in the workspace. Output is deterministic: fields appear in a fixed
//! order, so two runs of one program produce byte-identical JSON.

use crate::event::{ConflictReport, ConflictSide, TraceEventKind};
use crate::trace::Trace;
use lml_types::{Known, Value};

/// The trace JSON schema version. Bumped when the shape changes.
pub const TRACE_VERSION: u32 = 2;

/// Render a trace as JSON.
///
/// The wall-clock metadata is included when present, in a `metadata` object
/// that is clearly separate from `events` — semantic data and measurement do
/// not mix (owner instruction §10).
#[must_use]
pub fn to_json(trace: &Trace) -> String {
    let mut out = format!("{{\"trace_version\":{TRACE_VERSION}");

    if let Some(metadata) = trace.metadata() {
        out.push_str(&format!(
            ",\"metadata\":{{\"execution_id\":{},\"started_at_unix_nanos\":{},\"duration_nanos\":{}}}",
            string(&metadata.execution_id),
            metadata.started_at_unix_nanos,
            metadata.duration_nanos
        ));
    }

    out.push_str(",\"events\":[");
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
        TraceEventKind::FactDeclared { name, value }
        | TraceEventKind::OutputEmitted { name, value } => {
            format!(",\"name\":{},\"value\":{}", string(name), json_value(value))
        }
        TraceEventKind::RoundStarted { round } => format!(",\"round\":{round}"),
        TraceEventKind::ConditionEvaluated { rule, outcome } => {
            format!(
                ",\"rule\":{},\"outcome\":{}",
                string(rule),
                string(outcome.name())
            )
        }
        TraceEventKind::RulePending { rule, waiting_for } => {
            format!(
                ",\"rule\":{},\"waiting_for\":{}",
                string(rule),
                string_array(waiting_for)
            )
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
        TraceEventKind::ConflictRaised(report) => {
            format!(",\"conflict\":{}", json_conflict(report))
        }
        TraceEventKind::RoundFinished {
            round,
            fired_any,
            pending,
        } => {
            format!(",\"round\":{round},\"fired_any\":{fired_any},\"pending\":{pending}")
        }
        TraceEventKind::ExecutionFinished {
            rounds,
            facts_total,
            rules_fired,
            rules_pending,
        } => {
            format!(
                ",\"rounds\":{rounds},\"facts_total\":{facts_total},\"rules_fired\":{rules_fired},\"rules_pending\":{rules_pending}"
            )
        }
        TraceEventKind::ErrorRaised {
            code,
            message,
            cause,
        } => {
            let cause = cause
                .as_ref()
                .map_or_else(|| "null".to_owned(), |text| string(text));
            format!(
                ",\"code\":{},\"message\":{},\"cause\":{cause}",
                string(code),
                string(message)
            )
        }
    }
}

fn json_conflict(report: &ConflictReport) -> String {
    let facts: Vec<String> = report
        .relevant_facts
        .iter()
        .map(|(name, value)| {
            format!(
                "{{\"name\":{},\"value\":{}}}",
                string(name),
                json_value(value)
            )
        })
        .collect();
    let conditions: Vec<String> = report
        .relevant_conditions
        .iter()
        .map(|(rule, text)| {
            format!(
                "{{\"rule\":{},\"condition\":{}}}",
                string(rule),
                string(text)
            )
        })
        .collect();
    let refs: Vec<String> = report.trace_refs.iter().map(u64::to_string).collect();
    format!(
        "{{\"id\":{},\"name\":{},\"existing\":{},\"incoming\":{},\"relevant_facts\":[{}],\"relevant_conditions\":[{}],\"round\":{},\"trace_refs\":[{}]}}",
        string(&report.id),
        string(&report.name),
        json_side(&report.existing),
        json_side(&report.incoming),
        facts.join(","),
        conditions.join(","),
        report.round,
        refs.join(",")
    )
}

fn json_side(side: &ConflictSide) -> String {
    let rule = side
        .rule
        .as_ref()
        .map_or_else(|| "null".to_owned(), |text| string(text));
    format!(
        "{{\"rule\":{rule},\"value\":{},\"trace_seq\":{}}}",
        json_value(&side.value),
        side.trace_seq
    )
}

/// A value, tagged with its state and — when known — its type.
///
/// Public because it is *the* encoding of a value: the CLI and any other
/// consumer use this rather than writing a second one that could drift.
///
/// Tagged rather than bare for two reasons. `Unknown` and `Null` are distinct
/// states and JSON has one `null`, so a bare encoding would lose the
/// distinction. And JSON cannot tell `1` from `1.0`, which the language takes
/// seriously enough to refuse to compile `1 + 1.0`.
pub fn json_value(value: &Value) -> String {
    match value {
        Value::Unknown => "{\"state\":\"Unknown\"}".to_owned(),
        Value::Null => "{\"state\":\"Null\"}".to_owned(),
        Value::Known(known) => {
            let (ty, rendered) = match known {
                Known::Int(number) => ("Int", number.to_string()),
                // Always finite, so this is always valid JSON.
                Known::Float(number) => ("Float", format!("{number:?}")),
                Known::Bool(boolean) => ("Bool", boolean.to_string()),
                Known::Str(text) => ("String", string(text)),
            };
            format!("{{\"state\":\"Known\",\"type\":\"{ty}\",\"value\":{rendered}}}")
        }
    }
}

fn string_array(items: &[String]) -> String {
    let rendered: Vec<String> = items.iter().map(|item| string(item)).collect();
    format!("[{}]", rendered.join(","))
}

/// A JSON string literal, escaped per RFC 8259.
#[must_use]
pub fn json_string(text: &str) -> String {
    string(text)
}

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
    fn the_three_states_have_three_encodings() {
        // The requirement of owner instruction §9, executable: no state is
        // written as bare JSON `null`, and no two states share an encoding.
        let unknown = json_value(&Value::Unknown);
        let null = json_value(&Value::Null);
        let known = json_value(&Value::int(1));
        assert_eq!(unknown, "{\"state\":\"Unknown\"}");
        assert_eq!(null, "{\"state\":\"Null\"}");
        assert_eq!(known, "{\"state\":\"Known\",\"type\":\"Int\",\"value\":1}");
        assert_ne!(unknown, null);
        for encoding in [&unknown, &null, &known] {
            assert_ne!(
                encoding.as_str(),
                "null",
                "a state was encoded as bare JSON null"
            );
        }
    }

    #[test]
    fn values_carry_their_type() {
        assert_eq!(
            json_value(&Value::float(1.0).unwrap_or(Value::Null)),
            "{\"state\":\"Known\",\"type\":\"Float\",\"value\":1.0}"
        );
        assert_eq!(
            json_value(&Value::int(1)),
            "{\"state\":\"Known\",\"type\":\"Int\",\"value\":1}"
        );
    }

    #[test]
    fn control_characters_are_escaped() {
        assert_eq!(string("a\u{1}b"), "\"a\\u0001b\"");
        assert_eq!(string("tab\there"), "\"tab\\there\"");
    }

    #[test]
    fn an_empty_trace_is_still_valid_json() {
        assert_eq!(
            to_json(&Trace::new()),
            "{\"trace_version\":2,\"events\":[]}"
        );
    }
}
