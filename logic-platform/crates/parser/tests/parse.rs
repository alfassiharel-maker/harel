//! Parser tests against the grammar in `docs/02_FORMAL_SEMANTICS.md` §2.

// In a test, a panic is the reporting mechanism: `unwrap`, `expect` and
// `assert!` are how a failure is announced. They stay denied in library code
// (docs/03_EXECUTION_MODEL.md §8).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use lml_ast::print_program;
use lml_diagnostics::Code;
use lml_parser::parse_source;

fn tree(source: &str) -> String {
    print_program(&parse_source(source).unwrap_or_else(|d| panic!("expected a parse: {d}")))
}

fn codes(source: &str) -> Vec<Code> {
    parse_source(source)
        .expect_err(&format!("expected `{source}` to fail"))
        .as_slice()
        .iter()
        .map(|d| d.code)
        .collect()
}

#[test]
fn the_canonical_program() {
    let source = "fact temperature = 31\n\nrule heat:\n    when temperature > 30\n    then status = \"hot\"\n\noutput status\n";
    assert_eq!(
        tree(source),
        "(fact temperature 31)\n\
         (rule heat\n  \
           (when (> temperature 30))\n  \
           (then status \"hot\")\n\
         )\n\
         (output status)\n"
    );
}

#[test]
fn precedence_loosest_to_tightest() {
    assert_eq!(tree("fact a = 1 + 2 * 3").trim(), "(fact a (+ 1 (* 2 3)))");
    assert_eq!(
        tree("fact a = (1 + 2) * 3").trim(),
        "(fact a (* (+ 1 2) 3))"
    );
    assert_eq!(tree("fact a = -2 + 3").trim(), "(fact a (+ (- 2) 3))");
    assert_eq!(
        tree("rule r: when a > 1 and b > 2 or c > 3 then d = 1")
            .lines()
            .nth(1)
            .unwrap_or_default(),
        "  (when (or (and (> a 1) (> b 2)) (> c 3)))"
    );
    assert_eq!(
        tree("rule r: when not a and b then d = 1")
            .lines()
            .nth(1)
            .unwrap_or_default(),
        "  (when (and (not a) b))"
    );
}

#[test]
fn arithmetic_is_left_associative() {
    assert_eq!(tree("fact a = 1 - 2 - 3").trim(), "(fact a (- (- 1 2) 3))");
    assert_eq!(tree("fact a = 8 / 4 / 2").trim(), "(fact a (/ (/ 8 4) 2))");
}

#[test]
fn comparison_does_not_chain() {
    assert_eq!(
        codes("rule r: when a < b < c then d = 1"),
        vec![Code::ChainedComparison]
    );
}

#[test]
fn a_rule_needs_a_then_clause() {
    assert_eq!(codes("rule r: when a > 1"), vec![Code::RuleWithoutThen]);
}

#[test]
fn a_rule_may_derive_several_names() {
    assert_eq!(
        tree("rule r: when a > 1 then b = 2 then c = 3"),
        "(rule r\n  (when (> a 1))\n  (then b 2)\n  (then c 3)\n)\n"
    );
}

#[test]
fn output_takes_a_list() {
    assert_eq!(tree("output a, b, c").trim(), "(output a b c)");
}

#[test]
fn declarations_need_no_terminator() {
    // §2 — a declaration ends where the next keyword begins.
    assert_eq!(
        tree("fact a = 1 fact b = 2"),
        tree("fact a = 1\nfact b = 2\n")
    );
}

#[test]
fn declaration_order_is_preserved_but_free() {
    // A program is a set of declarations; `output` may precede what it names.
    assert_eq!(tree("output a fact a = 1"), "(output a)\n(fact a 1)\n");
}

#[test]
fn errors_resynchronise_at_the_next_declaration() {
    // The broken `fact` must not hide the broken `output`.
    let reported = codes("fact = 1\nfact good = 2\noutput ,");
    assert_eq!(reported, vec![Code::UnexpectedToken, Code::UnexpectedToken]);
}

#[test]
fn a_truncated_declaration_reports_end_of_input() {
    assert_eq!(codes("fact a ="), vec![Code::UnexpectedEndOfInput]);
}

#[test]
fn deep_nesting_is_bounded_not_a_stack_overflow() {
    let depth = lml_parser::MAX_EXPR_DEPTH + 10;
    let source = format!("fact a = {}1{}", "(".repeat(depth), ")".repeat(depth));
    assert_eq!(codes(&source), vec![Code::ExpressionTooDeep]);
}

#[test]
fn lexical_errors_are_reported_without_syntax_noise() {
    // A malformed literal must not also produce "expected an expression".
    assert_eq!(codes("fact a = 1_"), vec![Code::MalformedNumber]);
}

#[test]
fn spans_cover_the_whole_declaration() {
    let source = "fact temperature = 30 + 1";
    let program = parse_source(source).expect("parses");
    let span = program.items[0].span();
    assert_eq!(&source[span.start..span.end], source);
}
