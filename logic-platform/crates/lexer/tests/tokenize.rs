//! Lexer tests, against the public API only.
//!
//! Each test names the clause of `docs/02_FORMAL_SEMANTICS.md` §1 it covers.

// In a test, a panic is the reporting mechanism: `unwrap`, `expect` and
// `assert!` are how a failure is announced. They stay denied in library code
// (docs/03_EXECUTION_MODEL.md §8).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use lml_diagnostics::Code;
use lml_lexer::{tokenize, Token, TokenKind};

fn kinds(source: &str) -> Vec<TokenKind> {
    tokenize(source)
        .unwrap_or_else(|d| panic!("expected `{source}` to lex, got: {d}"))
        .into_iter()
        .map(|Token { kind, .. }| kind)
        .collect()
}

fn codes(source: &str) -> Vec<Code> {
    let diagnostics = tokenize(source).expect_err(&format!("expected `{source}` to fail lexing"));
    diagnostics.as_slice().iter().map(|d| d.code).collect()
}

#[test]
fn the_canonical_program_lexes() {
    let source = "fact temperature = 31\n\nrule heat:\n    when temperature > 30\n    then status = \"hot\"\n\noutput status\n";
    assert_eq!(
        kinds(source),
        vec![
            TokenKind::Fact,
            TokenKind::Ident("temperature".into()),
            TokenKind::Assign,
            TokenKind::Int(31),
            TokenKind::Rule,
            TokenKind::Ident("heat".into()),
            TokenKind::Colon,
            TokenKind::When,
            TokenKind::Ident("temperature".into()),
            TokenKind::Gt,
            TokenKind::Int(30),
            TokenKind::Then,
            TokenKind::Ident("status".into()),
            TokenKind::Assign,
            TokenKind::Str("hot".into()),
            TokenKind::Output,
            TokenKind::Ident("status".into()),
            TokenKind::Eof,
        ]
    );
}

#[test]
fn layout_is_not_significant() {
    // §1.2 — the same program on one line is the same token stream.
    let spread = "fact a = 1\n\n\trule r:\n\t\twhen a > 0\n\t\tthen b = 2\n";
    let flat = "fact a = 1 rule r: when a > 0 then b = 2";
    assert_eq!(kinds(spread), kinds(flat));
}

#[test]
fn comments_run_to_end_of_line() {
    // §1.3
    assert_eq!(
        kinds("# a comment\nfact a = 1 # another"),
        kinds("fact a = 1")
    );
}

#[test]
fn dotted_names_are_one_token() {
    // §1.4
    assert_eq!(kinds("user.age")[0], TokenKind::Ident("user.age".into()));
    assert_eq!(codes("user . age"), vec![Code::SpaceInDottedName]);
}

#[test]
fn keywords_are_not_names() {
    assert_eq!(kinds("true")[0], TokenKind::Bool(true));
    assert_eq!(codes("a.when"), vec![Code::ReservedWord]);
}

#[test]
fn future_keywords_are_reserved() {
    // §1.4 — so that adding them later is not a breaking change. `null` and
    // `unknown` were on this list and have now been spent: reserving them is
    // exactly what let the three-state model arrive without breaking anything.
    assert_eq!(codes("state"), vec![Code::ReservedWord]);
    assert_eq!(codes("query"), vec![Code::ReservedWord]);
    assert_eq!(codes("module"), vec![Code::ReservedWord]);
}

#[test]
fn the_state_vocabulary_lexes() {
    assert_eq!(kinds("null")[0], TokenKind::Null);
    assert_eq!(
        kinds("x is null"),
        vec![
            TokenKind::Ident("x".into()),
            TokenKind::Is,
            TokenKind::Null,
            TokenKind::Eof
        ]
    );
    assert_eq!(kinds("x is unknown")[2], TokenKind::Unknown);
    assert_eq!(kinds("x is known")[2], TokenKind::Known);
}

#[test]
fn numeric_separators() {
    assert_eq!(kinds("1_000")[0], TokenKind::Int(1000));
    assert_eq!(kinds("1_000.5")[0], TokenKind::Float(1000.5));
    assert_eq!(codes("1_"), vec![Code::MalformedNumber]);
    assert_eq!(codes("1__0"), vec![Code::MalformedNumber]);
}

#[test]
fn integers_out_of_range_are_rejected_not_wrapped() {
    assert_eq!(codes("9223372036854775808"), vec![Code::MalformedNumber]);
    assert_eq!(kinds("9223372036854775807")[0], TokenKind::Int(i64::MAX));
}

#[test]
fn a_float_needs_digits_after_the_point() {
    assert_eq!(codes("1."), vec![Code::MalformedNumber]);
}

#[test]
fn strings_and_escapes() {
    assert_eq!(kinds(r#""hot""#)[0], TokenKind::Str("hot".into()));
    assert_eq!(kinds(r#""a\"b\n""#)[0], TokenKind::Str("a\"b\n".into()));
    assert_eq!(codes("\"unclosed"), vec![Code::UnterminatedString]);
    // A string may not span lines. The rest of the line still scans, so the
    // stray closing quote is reported as unterminated in its turn — a cascade
    // from one mistake, which is preferable to swallowing the rest of the file.
    assert_eq!(codes("\"over\nlines\"")[0], Code::UnterminatedString);
    assert_eq!(codes(r#""\q""#), vec![Code::InvalidEscape]);
}

#[test]
fn non_ascii_is_only_allowed_in_strings_and_comments() {
    // §1.1
    assert!(tokenize("# מזג אוויר\nfact a = \"חם\"").is_ok());
    let reported = codes("fact מזג = 1");
    assert!(
        reported.iter().all(|&c| c == Code::UnexpectedCharacter),
        "{reported:?}"
    );
}

#[test]
fn maximal_munch() {
    assert_eq!(kinds("a <= b")[1], TokenKind::Le);
    assert_eq!(kinds("a < b")[1], TokenKind::Lt);
    assert_eq!(kinds("a == b")[1], TokenKind::Eq);
    assert_eq!(kinds("a != b")[1], TokenKind::Ne);
}

#[test]
fn errors_do_not_stop_scanning() {
    // Every lexical problem is reported in one pass.
    assert_eq!(
        codes("fact a = 1_ fact b = $"),
        vec![Code::MalformedNumber, Code::UnknownToken]
    );
}

#[test]
fn spans_point_at_the_token() {
    let source = "fact temperature = 31";
    let tokens = tokenize(source).expect("lexes");
    let int = tokens
        .iter()
        .find(|t| matches!(t.kind, TokenKind::Int(_)))
        .expect("has an int");
    assert_eq!(&source[int.span.start..int.span.end], "31");
}

#[test]
fn empty_source_is_just_eof() {
    assert_eq!(kinds(""), vec![TokenKind::Eof]);
    assert_eq!(kinds("   \n\n # only a comment\n"), vec![TokenKind::Eof]);
}
