//! Lowering tests, including the canonical IR snapshot.

// In a test, a panic is the reporting mechanism: `unwrap`, `expect` and
// `assert!` are how a failure is announced. They stay denied in library code
// (docs/03_EXECUTION_MODEL.md §8).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use lml_compiler::compile;
use lml_diagnostics::Code;
use lml_ir::print_ir;

const CANONICAL: &str = "fact temperature = 31\n\
                         rule heat:\n    when temperature > 30\n    then status = \"hot\"\n\
                         output status\n";

fn codes(source: &str) -> Vec<Code> {
    compile(source)
        .err()
        .unwrap_or_else(|| panic!("expected `{source}` to be rejected"))
        .as_slice()
        .iter()
        .map(|d| d.code)
        .collect()
}

#[test]
fn the_canonical_program_lowers_to_this_ir() {
    // A snapshot: an unintended change to lowering shows up as a diff here
    // rather than as a change in behaviour somewhere else.
    let compilation = compile(CANONICAL).unwrap_or_else(|d| panic!("compiles: {d}"));
    assert_eq!(
        print_ir(&compilation.ir),
        "ir_version 2\n\
         names\n  \
           0 temperature : Int\n  \
           1 status : String\n\
         facts\n  \
           temperature = 31\n\
         rules\n  \
           r0 heat\n    \
             reads temperature\n    \
             when [load temperature; const 30; gt]\n    \
             then status = [const \"hot\"]\n\
         outputs\n  \
           status\n"
    );
}

#[test]
fn lowering_is_deterministic() {
    let first = compile(CANONICAL).unwrap_or_else(|d| panic!("compiles: {d}"));
    let second = compile(CANONICAL).unwrap_or_else(|d| panic!("compiles: {d}"));
    assert_eq!(print_ir(&first.ir), print_ir(&second.ir));
    assert_eq!(first.ir, second.ir);
}

#[test]
fn a_fact_value_is_folded_to_a_constant() {
    let compilation = compile("fact a = 2 * 3 + 1\noutput a").unwrap_or_else(|d| panic!("{d}"));
    assert_eq!(compilation.ir.facts[0].value.to_string(), "7");
}

#[test]
fn null_lowers_as_a_constant_and_the_state_tests_as_ops() {
    let compilation = compile(
        "fact missing = null\n\
         rule check: when missing is null then note = missing is known\n\
         output note",
    )
    .unwrap_or_else(|d| panic!("compiles: {d}"));
    let text = print_ir(&compilation.ir);
    assert!(text.contains("missing = null"), "{text}");
    assert!(text.contains("when [load missing; is-null]"), "{text}");
    assert!(
        text.contains("then note = [load missing; is-known]"),
        "{text}"
    );
}

#[test]
fn a_null_writer_does_not_conflict_with_a_typed_one() {
    // `Null` unifies with every type: a name that is sometimes absent and
    // sometimes an `Int` is an `Int` that can be absent.
    let compilation = compile(
        "fact a = 1\n\
         rule some: when a > 0 then result = 5\n\
         rule none: when a > 99 then result = null\n\
         output result",
    )
    .unwrap_or_else(|d| panic!("compiles: {d}"));
    assert!(print_ir(&compilation.ir).contains("result : Int"));
}

#[test]
fn null_still_has_no_magnitude_at_compile_time() {
    assert_eq!(
        codes("fact a = null + 1\noutput a"),
        vec![Code::OperatorTypeMismatch]
    );
    assert_eq!(
        codes("fact a = 1\nrule r: when a > null then b = 1\noutput b"),
        vec![Code::OperatorTypeMismatch]
    );
}

#[test]
fn a_fact_may_not_read_another_name() {
    assert_eq!(
        codes("fact a = 1\nfact b = a + 1\noutput b"),
        vec![Code::NonConstantFact]
    );
}

#[test]
fn constant_folding_reports_arithmetic_failures_at_compile_time() {
    assert_eq!(
        codes("fact a = 1 / 0\noutput a"),
        vec![Code::DivisionByZero]
    );
    assert_eq!(
        codes("fact a = 9223372036854775807 + 1\noutput a"),
        vec![Code::IntegerOverflow]
    );
}

#[test]
fn reads_cover_the_condition_and_the_consequences() {
    let source = "fact a = 1\nfact b = 2\nrule r: when a > 0 then c = b + 1\noutput c";
    let compilation = compile(source).unwrap_or_else(|d| panic!("{d}"));
    let rule = &compilation.ir.rules[0];
    let reads: Vec<String> = rule
        .reads
        .iter()
        .map(|&id| compilation.ir.name_text(id))
        .collect();
    assert_eq!(reads, vec!["a".to_owned(), "b".to_owned()]);
}

#[test]
fn every_expression_lowers_to_balanced_code() {
    let source = "fact a = 1\n\
                  rule r: when not (a > 0 and a < 10) or a == 5 then b = -a % 3\n\
                  output b";
    let compilation = compile(source).unwrap_or_else(|d| panic!("{d}"));
    let rule = &compilation.ir.rules[0];
    assert!(rule.condition.stack_depth() >= 1);
    assert!(rule.effects[0].code.stack_depth() >= 1);
}

#[test]
fn the_first_failing_stage_is_the_only_one_reported() {
    // A syntax error must not also produce type errors.
    assert_eq!(codes("fact a ="), vec![Code::UnexpectedEndOfInput]);
}
