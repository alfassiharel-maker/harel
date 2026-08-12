//! Execution tests: the dynamic semantics of `docs/02_FORMAL_SEMANTICS.md` §4.

// In a test, a panic is the reporting mechanism: `unwrap`, `expect` and
// `assert!` are how a failure is announced. They stay denied in library code
// (docs/03_EXECUTION_MODEL.md §8).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use lml_compiler::compile;
use lml_diagnostics::Code;
use lml_runtime::{run, Execution, Limits};
use lml_trace::{why, Emitted};
use lml_types::Value;

fn execute(source: &str) -> Execution {
    let compilation = compile(source).unwrap_or_else(|d| panic!("compiles: {d}"));
    run(&compilation.ir, Limits::new()).unwrap_or_else(|f| panic!("runs: {}", f.diagnostic))
}

fn failure(source: &str) -> Code {
    let compilation = compile(source).unwrap_or_else(|d| panic!("compiles: {d}"));
    run(&compilation.ir, Limits::new())
        .err()
        .unwrap_or_else(|| panic!("expected `{source}` to fail"))
        .diagnostic
        .code
}

#[test]
fn the_canonical_program_produces_the_specified_result() {
    // docs/01_LANGUAGE_CONSTITUTION.md §2 — the program the specification names.
    let execution = execute(
        "fact temperature = 31\n\
         rule heat:\n    when temperature > 30\n    then status = \"hot\"\n\
         output status\n",
    );
    assert_eq!(execution.to_text(), "status = \"hot\"");
    assert_eq!(
        execution.output("status"),
        Some(&Emitted::Known(Value::Str("hot".into())))
    );
    assert_eq!(execution.rules_fired, 1);
}

#[test]
fn the_canonical_program_explains_itself() {
    let execution = execute(
        "fact temperature = 31\n\
         rule heat: when temperature > 30 then status = \"hot\"\n\
         output status\n",
    );
    let explanation = why(&execution.trace, "status").expect("the trace supports it");
    assert_eq!(
        explanation.to_text(),
        "status = \"hot\" by rule `heat`\n  temperature = 31 declared\n"
    );
}

#[test]
fn a_rule_whose_condition_is_false_does_not_fire() {
    let execution = execute(
        "fact temperature = 20\n\
         rule heat: when temperature > 30 then status = \"hot\"\n\
         output status\n",
    );
    // Not `0`, not `""`, not an error: the engine says it does not know.
    assert_eq!(execution.to_text(), "status = unknown");
    assert_eq!(execution.rules_fired, 0);
}

#[test]
fn inference_chains_to_a_fixed_point() {
    let execution = execute(
        "fact a = 1\n\
         rule r1: when a > 0 then b = a + 1\n\
         rule r2: when b > 1 then c = b + 1\n\
         rule r3: when c > 2 then d = c + 1\n\
         output b, c, d\n",
    );
    assert_eq!(execution.to_text(), "b = 2\nc = 3\nd = 4");
}

#[test]
fn a_fact_derived_earlier_in_a_round_is_visible_later_in_it() {
    // §4.3 — which is what makes the fixed point independent of how rounds are
    // cut, and why this program settles in one round of work.
    let execution = execute(
        "fact a = 1\n\
         rule r1: when a > 0 then b = 2\n\
         rule r2: when b > 1 then c = 3\n\
         output c\n",
    );
    assert_eq!(execution.to_text(), "c = 3");
    assert_eq!(
        execution.rounds, 2,
        "one round of work, one round to observe the fixed point"
    );
}

#[test]
fn rule_order_in_the_source_does_not_change_the_result() {
    let forwards = execute(
        "fact a = 1\nrule r1: when a > 0 then b = 2\nrule r2: when b > 1 then c = 3\noutput c",
    );
    let backwards = execute(
        "fact a = 1\nrule r2: when b > 1 then c = 3\nrule r1: when a > 0 then b = 2\noutput c",
    );
    assert_eq!(forwards.to_text(), backwards.to_text());
}

#[test]
fn an_unknown_read_makes_a_rule_pending_not_false() {
    // `not_yet` is never derived, so `waiting` never fires — and `blocked`
    // stays unknown rather than becoming false.
    let execution = execute(
        "fact a = 1\n\
         rule impossible: when a > 99 then not_yet = 1\n\
         rule waiting: when not_yet > 0 then blocked = 1\n\
         output blocked\n",
    );
    assert_eq!(execution.to_text(), "blocked = unknown");
}

#[test]
fn two_rules_agreeing_is_not_a_conflict() {
    let execution = execute(
        "fact a = 1\n\
         rule r1: when a > 0 then s = 5\n\
         rule r2: when a > 0 then s = 5\n\
         output s\n",
    );
    assert_eq!(execution.to_text(), "s = 5");
}

#[test]
fn two_rules_disagreeing_is_an_error() {
    let source = "fact a = 1\n\
                  rule r1: when a > 0 then s = 5\n\
                  rule r2: when a > 0 then s = 6\n\
                  output s\n";
    assert_eq!(failure(source), Code::ConflictingDerivation);
}

#[test]
fn runtime_arithmetic_failures_are_errors_not_wrapped_values() {
    let overflow = "fact a = 9223372036854775807\n\
                    rule r: when a > 0 then b = a + 1\n\
                    output b";
    assert_eq!(failure(overflow), Code::IntegerOverflow);

    let zero = "fact a = 0\nrule r: when a == 0 then b = 1 / a\noutput b";
    assert_eq!(failure(zero), Code::DivisionByZero);
}

#[test]
fn a_failure_still_produces_the_trace_up_to_it() {
    let compilation = compile("fact a = 0\nrule r: when a == 0 then b = 1 / a\noutput b")
        .unwrap_or_else(|d| panic!("compiles: {d}"));
    let failure = run(&compilation.ir, Limits::new()).expect_err("fails");
    assert!(failure.trace.to_text().contains("FactDeclared a = 0"));
    assert!(failure.trace.to_text().contains("ErrorRaised E5002"));
}

#[test]
fn the_round_limit_is_reported_not_hung() {
    let compilation = compile("fact a = 1\nrule r: when a > 0 then b = 2\noutput b")
        .unwrap_or_else(|d| panic!("compiles: {d}"));
    // One round is not enough to reach the fixed point: round 1 fires `r`, and
    // a second round is needed to observe that nothing more fires.
    let failure = run(&compilation.ir, Limits::new().with_max_inference_rounds(1))
        .expect_err("hits the limit");
    assert_eq!(failure.diagnostic.code, Code::InferenceRoundLimit);
}

#[test]
fn execution_is_deterministic_down_to_the_trace() {
    let source = "fact a = 1\n\
                  rule r1: when a > 0 then b = a + 1\n\
                  rule r2: when b > 1 then c = \"x\" + \"y\"\n\
                  output c, b\n";
    let first = execute(source);
    let second = execute(source);
    assert_eq!(first.trace, second.trace);
    assert_eq!(first.facts, second.facts);
    assert_eq!(first.to_text(), second.to_text());
}

#[test]
fn a_value_cycle_reaches_a_fixed_point() {
    // docs/01 §5 — a cyclic dependency needs no special handling: monotone
    // iteration over immutable facts terminates on its own.
    let execution = execute(
        "fact seed = 1\n\
         rule r1: when seed > 0 and b > 0 then a = 1\n\
         rule r2: when seed > 0 and a > 0 then b = 2\n\
         output a, b\n",
    );
    // Neither rule can ever fire: each waits for the other's output. That is a
    // fixed point, not a hang.
    assert_eq!(execution.to_text(), "a = unknown\nb = unknown");
}
