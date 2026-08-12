//! The grammar file and the formal semantics must agree.
//!
//! `grammar.ebnf` claimed this test existed before it did — the design audit
//! recorded that as contradiction C7. Now it does: the EBNF block in
//! `docs/02_FORMAL_SEMANTICS.md` §2 and `crates/parser/grammar.ebnf` are
//! compared production by production, so the specification and the file the
//! parser is written against cannot drift.

// In a test, a panic is the reporting mechanism.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::path::Path;

/// Productions, normalised: comments stripped, whitespace collapsed, blank
/// lines dropped. Layout differences between the two files are not drift.
fn productions(text: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut in_comment = false;
    for raw in text.lines() {
        let mut line = raw.to_owned();
        // EBNF comments: `(* ... *)`, possibly spanning lines.
        loop {
            if in_comment {
                match line.find("*)") {
                    Some(end) => {
                        line = line[end + 2..].to_owned();
                        in_comment = false;
                    }
                    None => {
                        line.clear();
                        break;
                    }
                }
            }
            match line.find("(*") {
                Some(start) => {
                    let tail = line[start..].to_owned();
                    line.truncate(start);
                    in_comment = true;
                    if let Some(end) = tail.find("*)") {
                        line.push_str(&tail[end + 2..]);
                        in_comment = false;
                    } else {
                        break;
                    }
                }
                None => break,
            }
        }
        out.push(line);
    }
    // Productions end at `;`, not at a line break, so a production split over
    // two lines for readability is the same production.
    out.join(" ")
        .split(';')
        .map(|production| production.split_whitespace().collect::<Vec<_>>().join(" "))
        .filter(|production| !production.is_empty())
        .collect()
}

/// The ```ebnf fenced block of `docs/02_FORMAL_SEMANTICS.md` §2.
fn documented_grammar() -> String {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../docs/02_FORMAL_SEMANTICS.md");
    let document = std::fs::read_to_string(&path)
        .unwrap_or_else(|error| panic!("cannot read {}: {error}", path.display()));
    let start = document
        .find("```ebnf")
        .expect("docs/02 §2 has an ```ebnf block");
    let rest = &document[start + "```ebnf".len()..];
    let end = rest.find("```").expect("the ebnf block is closed");
    rest[..end].to_owned()
}

fn grammar_file() -> String {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("grammar.ebnf");
    std::fs::read_to_string(&path)
        .unwrap_or_else(|error| panic!("cannot read {}: {error}", path.display()))
}

#[test]
fn the_grammar_file_matches_the_formal_semantics() {
    let documented = productions(&documented_grammar());
    let implemented = productions(&grammar_file());

    assert!(!documented.is_empty(), "no productions found in docs/02 §2");
    assert_eq!(
        documented, implemented,
        "crates/parser/grammar.ebnf and docs/02_FORMAL_SEMANTICS.md §2 disagree.\n\
         Change both, or neither: the grammar is a language decision."
    );
}

#[test]
fn every_production_the_parser_implements_is_written_down() {
    // A cheap completeness check: each named non-terminal the parser has a
    // function for must appear on the left of a production.
    let grammar = grammar_file();
    for production in [
        "program",
        "item",
        "fact_decl",
        "rule_decl",
        "then_clause",
        "output_decl",
        "expr",
        "or_expr",
        "and_expr",
        "not_expr",
        "comparison",
        "state_test",
        "additive",
        "multiplicative",
        "unary",
        "primary",
    ] {
        assert!(
            grammar.contains(&format!("{production} ")),
            "`{production}` is implemented but not written down in grammar.ebnf"
        );
    }
}
