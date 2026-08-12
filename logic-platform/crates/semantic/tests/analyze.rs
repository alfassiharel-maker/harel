//! Static-analysis tests, one per clause of `docs/02_FORMAL_SEMANTICS.md` §3.

// In a test, a panic is the reporting mechanism: `unwrap`, `expect` and
// `assert!` are how a failure is announced. They stay denied in library code
// (docs/03_EXECUTION_MODEL.md §8).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use lml_diagnostics::Code;
use lml_semantic::{analyze, SemanticModel};
use lml_types::Type;

fn model(source: &str) -> SemanticModel {
    let program = lml_parser::parse_source(source).unwrap_or_else(|d| panic!("parses: {d}"));
    analyze(program).unwrap_or_else(|d| panic!("analyses: {d}"))
}

fn codes(source: &str) -> Vec<Code> {
    let program = lml_parser::parse_source(source).unwrap_or_else(|d| panic!("parses: {d}"));
    analyze(program)
        .err()
        .unwrap_or_else(|| panic!("expected `{source}` to be rejected"))
        .as_slice()
        .iter()
        .map(|d| d.code)
        .collect()
}

#[test]
fn the_canonical_program_analyses() {
    let model = model("fact temperature = 31\nrule heat:\n when temperature > 30\n then status = \"hot\"\noutput status\n");
    assert_eq!(model.type_of("temperature"), Some(Type::Int));
    assert_eq!(model.type_of("status"), Some(Type::String));
    assert_eq!(model.outputs, vec!["status".to_owned()]);
    assert_eq!(
        model.rules[0].reads.iter().cloned().collect::<Vec<_>>(),
        vec!["temperature"]
    );
    assert_eq!(model.rules[0].writes, vec!["status".to_owned()]);
}

#[test]
fn n1_duplicate_fact() {
    assert_eq!(
        codes("fact a = 1\nfact a = 2\noutput a"),
        vec![Code::DuplicateFact]
    );
}

#[test]
fn n2_duplicate_rule() {
    let source =
        "fact a = 1\nrule r: when a > 0 then b = 1\nrule r: when a > 0 then c = 1\noutput b";
    assert_eq!(codes(source), vec![Code::DuplicateRule]);
}

#[test]
fn n3_unknown_name() {
    assert_eq!(codes("fact a = b"), vec![Code::UnknownName]);
    assert_eq!(
        codes("fact a = 1\nrule r: when missing > 1 then b = 2\noutput b"),
        vec![Code::UnknownName]
    );
}

#[test]
fn n4_a_rule_may_not_shadow_a_fact() {
    let source = "fact a = 1\nrule r: when a > 0 then a = 2\noutput a";
    assert_eq!(codes(source), vec![Code::DerivationShadowsFact]);
}

#[test]
fn n5_output_of_an_unknown_name() {
    assert_eq!(codes("fact a = 1\noutput b"), vec![Code::UnknownName]);
}

#[test]
fn n6_duplicate_output() {
    assert_eq!(
        codes("fact a = 1\noutput a, a"),
        vec![Code::DuplicateOutput]
    );
    assert_eq!(
        codes("fact a = 1\noutput a\noutput a"),
        vec![Code::DuplicateOutput]
    );
}

#[test]
fn declaration_order_does_not_matter() {
    let model = model(
        "output status\nrule heat: when temperature > 30 then status = 1\nfact temperature = 31",
    );
    assert_eq!(model.type_of("status"), Some(Type::Int));
}

#[test]
fn t1_writers_of_one_name_must_agree_on_its_type() {
    let source =
        "fact a = 1\nrule r: when a > 0 then s = 1\nrule q: when a > 0 then s = \"x\"\noutput s";
    assert_eq!(codes(source), vec![Code::ConflictingNameType]);
}

#[test]
fn t2_a_condition_must_be_boolean() {
    assert_eq!(
        codes("fact a = 1\nrule r: when a then b = 1\noutput b"),
        vec![Code::NonBooleanCondition]
    );
    // ...and there is no truthiness for integers either.
    assert_eq!(
        codes("fact a = 1\nrule r: when 1 then b = 1\noutput b"),
        vec![Code::NonBooleanCondition]
    );
}

#[test]
fn t3_and_t4_operators_are_typed_with_no_conversion() {
    assert_eq!(codes("fact a = 1 + 1.0"), vec![Code::OperatorTypeMismatch]);
    assert_eq!(
        codes("fact a = \"x\" - \"y\""),
        vec![Code::OperatorTypeMismatch]
    );
    assert_eq!(codes("fact a = not 1"), vec![Code::OperatorTypeMismatch]);
    assert_eq!(codes("fact a = 1 == 1.0"), vec![Code::OperatorTypeMismatch]);
}

#[test]
fn strings_concatenate() {
    assert_eq!(
        model("fact a = \"x\" + \"y\"\noutput a").type_of("a"),
        Some(Type::String)
    );
}

#[test]
fn t5_a_type_may_not_depend_on_itself() {
    let source = "fact seed = 1\nrule r: when seed > 0 then a = b\nrule q: when seed > 0 then b = a\noutput a";
    assert_eq!(codes(source), vec![Code::CyclicTypeDependency]);
}

#[test]
fn a_value_cycle_is_fine_when_the_types_are_grounded() {
    // Values may depend on one another (docs/01 §5); only types may not.
    let source = "fact seed = 1\n\
                  rule r: when seed > 0 and b > 0 then a = 1\n\
                  rule q: when seed > 0 and a > 0 then b = 2\n\
                  output a, b";
    let model = model(source);
    assert_eq!(model.type_of("a"), Some(Type::Int));
}

#[test]
fn every_error_is_reported_not_just_the_first() {
    let reported = codes("fact a = 1\nfact a = 2\noutput missing");
    assert_eq!(reported, vec![Code::DuplicateFact, Code::UnknownName]);
}

#[test]
fn one_mistake_gives_one_diagnostic() {
    // A name whose type could not be inferred must not also produce operator
    // errors everywhere it is used.
    let source = "fact seed = 1\n\
                  rule r: when seed > 0 then bad = 1 + 1.0\n\
                  rule q: when bad > 0 then worse = bad + 1\n\
                  output worse";
    assert_eq!(codes(source), vec![Code::OperatorTypeMismatch]);
}

#[test]
fn readers_of_gives_the_dependency_graph() {
    let model =
        model("fact a = 1\nrule r: when a > 0 then b = 1\nrule q: when b > 0 then c = 1\noutput c");
    assert_eq!(model.readers_of("a"), vec![0]);
    assert_eq!(model.readers_of("b"), vec![1]);
    assert_eq!(model.readers_of("c"), Vec::<usize>::new());
}
