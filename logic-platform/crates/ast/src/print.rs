//! Canonical printing of a syntax tree.
//!
//! The format is a fully parenthesised s-expression: unambiguous, stable, and
//! diffable, which is what makes it usable as a snapshot (`docs/05_ARCHITECTURE.md`
//! §5). It is a debugging and tooling format, not source code — it does not
//! round-trip through the parser and is not meant to.

use crate::expr::{Expr, Literal};
use crate::program::{Item, Program};

/// Render a program in the canonical form.
#[must_use]
pub fn print_program(program: &Program) -> String {
    let mut out = String::new();
    for item in &program.items {
        match item {
            Item::Fact(decl) => {
                out.push_str(&format!(
                    "(fact {} {})\n",
                    decl.name,
                    print_expr(&decl.value)
                ));
            }
            Item::Rule(decl) => {
                out.push_str(&format!("(rule {}\n", decl.name));
                out.push_str(&format!("  (when {})\n", print_expr(&decl.condition)));
                for effect in &decl.effects {
                    out.push_str(&format!(
                        "  (then {} {})\n",
                        effect.name,
                        print_expr(&effect.value)
                    ));
                }
                out.push_str(")\n");
            }
            Item::Output(decl) => {
                let names: Vec<String> = decl.names.iter().map(ToString::to_string).collect();
                out.push_str(&format!("(output {})\n", names.join(" ")));
            }
        }
    }
    out
}

/// Render one expression in the canonical form.
#[must_use]
pub fn print_expr(expr: &Expr) -> String {
    match expr {
        Expr::Literal { value, .. } => print_literal(value),
        Expr::Name(name) => name.text.clone(),
        Expr::Unary { op, operand, .. } => format!("({} {})", op.symbol(), print_expr(operand)),
        Expr::Is { operand, test, .. } => {
            format!("(is-{} {})", test.keyword(), print_expr(operand))
        }
        Expr::Binary {
            op, left, right, ..
        } => {
            format!(
                "({} {} {})",
                op.symbol(),
                print_expr(left),
                print_expr(right)
            )
        }
    }
}

/// Render a literal exactly as the engine holds it.
///
/// Floats print with a decimal point even when integral (`1.0`, not `1`) so
/// that the printed form never blurs `Int` and `Float` — a distinction the type
/// system takes seriously (`docs/04_TYPE_AND_DATA_MODEL.md` §3).
#[must_use]
pub fn print_literal(literal: &Literal) -> String {
    match literal {
        Literal::Int(value) => value.to_string(),
        Literal::Float(value) => {
            let text = value.to_string();
            if text.contains('.') {
                text
            } else {
                format!("{text}.0")
            }
        }
        Literal::Bool(value) => value.to_string(),
        Literal::Str(value) => print_string(value),
        Literal::Null => "null".to_owned(),
    }
}

/// Render a string with the language's escapes, so the output is re-readable.
#[must_use]
pub fn print_string(value: &str) -> String {
    let mut out = String::with_capacity(value.len() + 2);
    out.push('"');
    for ch in value.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\t' => out.push_str("\\t"),
            '\r' => out.push_str("\\r"),
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
    fn floats_never_print_as_integers() {
        assert_eq!(print_literal(&Literal::Float(1.0)), "1.0");
        assert_eq!(print_literal(&Literal::Float(1.5)), "1.5");
        assert_eq!(print_literal(&Literal::Int(1)), "1");
    }

    #[test]
    fn strings_round_trip_their_escapes() {
        assert_eq!(print_literal(&Literal::Str("a\"b\n".into())), r#""a\"b\n""#);
    }
}
