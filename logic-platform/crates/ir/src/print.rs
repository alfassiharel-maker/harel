//! Canonical IR text.
//!
//! Stable, ordered and free of source positions, so it can be diffed as a
//! snapshot and hashed as a program digest. Two programs with the same
//! behaviour and the same names produce the same text; any change to lowering
//! shows up as a diff rather than as a silent change in behaviour.

use crate::code::{ExprCode, Op};
use crate::program::IrProgram;

/// Render a program in the canonical form.
#[must_use]
pub fn print_ir(program: &IrProgram) -> String {
    let mut out = String::new();
    out.push_str(&format!("ir_version {}\n", program.ir_version));

    out.push_str("names\n");
    for (id, name) in program.names.iter() {
        let ty = program
            .type_of(id)
            .map_or_else(|| "?".to_owned(), |ty| ty.to_string());
        out.push_str(&format!("  {} {name} : {ty}\n", id.index()));
    }

    out.push_str("facts\n");
    for fact in &program.facts {
        out.push_str(&format!(
            "  {} = {}\n",
            program.name_text(fact.name),
            fact.value
        ));
    }

    out.push_str("rules\n");
    for rule in &program.rules {
        out.push_str(&format!("  {} {}\n", rule.id, rule.name));
        let reads: Vec<String> = rule.reads.iter().map(|&id| program.name_text(id)).collect();
        out.push_str(&format!("    reads {}\n", reads.join(" ")));
        out.push_str(&format!(
            "    when {}\n",
            print_code(&rule.condition, program)
        ));
        for effect in &rule.effects {
            out.push_str(&format!(
                "    then {} = {}\n",
                program.name_text(effect.name),
                print_code(&effect.code, program)
            ));
        }
    }

    out.push_str("outputs\n");
    for &id in &program.outputs {
        out.push_str(&format!("  {}\n", program.name_text(id)));
    }

    out
}

/// Render one expression's operations, space-separated.
#[must_use]
pub fn print_code(code: &ExprCode, program: &IrProgram) -> String {
    let rendered: Vec<String> = code
        .ops()
        .iter()
        .map(|op| match op {
            Op::Const(value) => format!("const {value}"),
            Op::Load(id) => format!("load {}", program.name_text(*id)),
            other => other.mnemonic().to_owned(),
        })
        .collect();
    format!("[{}]", rendered.join("; "))
}
