//! Lowering: semantic model to IR.
//!
//! Two things happen here and nowhere else: expressions become post-order
//! operation sequences, and a declared fact's value is folded to a constant.
//! Everything else is a rearrangement of what the semantic model already
//! established.

use lml_ast::{BinaryOp, Expr, Literal, UnaryOp};
use lml_diagnostics::{Code, Diagnostic, Diagnostics, Span};
use lml_ir::{
    ExprCode, IrEffect, IrFact, IrProgram, IrRule, NameId, NameTable, Op, RuleId, IR_VERSION,
};
use lml_logic::{eval, Evaluated, FactSet};
use lml_semantic::SemanticModel;
use lml_types::{Type, Value};

/// Lower a validated model to IR.
///
/// # Errors
/// Constant folding can fail — `fact a = 1 / 0` is a division by zero the
/// engine reports at compile time rather than at run time — and a lowering
/// defect is reported as `E5099` rather than panicking.
pub fn lower(model: &SemanticModel) -> Result<IrProgram, Diagnostics> {
    let mut diagnostics = Diagnostics::new();
    let mut names = NameTable::new();

    // Interning order is a source-order walk, so the same program always
    // produces the same table and therefore the same IR text and digest.
    for fact in model.program.facts() {
        names.intern(&fact.name.text);
    }
    for rule in model.program.rules() {
        intern_expr(&mut names, &rule.condition);
        for effect in &rule.effects {
            names.intern(&effect.name.text);
            intern_expr(&mut names, &effect.value);
        }
    }
    for name in &model.outputs {
        names.intern(name);
    }

    let facts = lower_facts(model, &mut names, &mut diagnostics);
    let rules = lower_rules(model, &mut names, &mut diagnostics);

    let mut types = vec![Type::Bool; names.len()];
    for (id, text) in names.iter() {
        if let Some(ty) = model.type_of(text) {
            // Cannot fail: the vector was sized from the same table.
            if let Some(slot) = types.get_mut(id.index()) {
                *slot = ty;
            }
        }
    }

    let outputs = model
        .outputs
        .iter()
        .map(|name| names.intern(name))
        .collect();

    diagnostics.sort_by_position();
    diagnostics.into_result(IrProgram {
        ir_version: IR_VERSION,
        names,
        types,
        facts,
        rules,
        outputs,
    })
}

fn lower_facts(
    model: &SemanticModel,
    names: &mut NameTable,
    diagnostics: &mut Diagnostics,
) -> Vec<IrFact> {
    let mut facts = Vec::new();
    for fact in model.program.facts() {
        let Some(code) = build(&fact.value, names, diagnostics) else {
            continue;
        };
        // A fact's value is constant (`E3006` rejects anything else), so the
        // empty fact set is enough to evaluate it.
        match eval(&code, &FactSet::new(), fact.value.span()) {
            Ok(Evaluated::Known(value)) => {
                facts.push(IrFact {
                    name: names.intern(&fact.name.text),
                    value,
                    span: fact.span,
                });
            }
            Ok(Evaluated::NotEvaluable) => diagnostics.push(Diagnostic::new(
                Code::InternalInvariant,
                fact.value.span(),
                "a fact's value was not constant after the semantic check accepted it",
            )),
            Err(diagnostic) => diagnostics.push(diagnostic),
        }
    }
    facts
}

fn lower_rules(
    model: &SemanticModel,
    names: &mut NameTable,
    diagnostics: &mut Diagnostics,
) -> Vec<IrRule> {
    let mut rules = Vec::new();
    for (index, rule) in model.program.rules().enumerate() {
        let Some(condition) = build(&rule.condition, names, diagnostics) else {
            continue;
        };

        let mut effects = Vec::new();
        for effect in &rule.effects {
            let Some(code) = build(&effect.value, names, diagnostics) else {
                continue;
            };
            effects.push(IrEffect {
                name: names.intern(&effect.name.text),
                code,
                span: effect.span,
            });
        }

        // Reads are collected over the whole rule — condition and consequences
        // alike — which is what makes a firing rule's consequences always
        // evaluable (`docs/02_FORMAL_SEMANTICS.md` §4.2).
        let info = model.rules.get(index);
        let mut reads: Vec<NameId> = info
            .map(|info| info.reads.iter().map(|name| names.intern(name)).collect())
            .unwrap_or_default();
        reads.sort_unstable();
        reads.dedup();

        let writes = effects.iter().map(|effect| effect.name).collect();

        rules.push(IrRule {
            id: RuleId(u32::try_from(index).unwrap_or(u32::MAX)),
            name: rule.name.text.clone(),
            condition,
            effects,
            reads,
            writes,
            span: rule.span,
        });
    }
    rules
}

/// Intern every name an expression reads, in source order.
fn intern_expr(names: &mut NameTable, expr: &Expr) {
    expr.visit_names(&mut |name| {
        names.intern(&name.text);
    });
}

/// Lower one expression, or report why it could not be lowered.
fn build(expr: &Expr, names: &mut NameTable, diagnostics: &mut Diagnostics) -> Option<ExprCode> {
    let mut ops = Vec::new();
    emit(expr, names, &mut ops, diagnostics)?;
    match ExprCode::new(ops) {
        Ok(code) => Some(code),
        Err(error) => {
            diagnostics.push(Diagnostic::new(
                Code::InternalInvariant,
                expr.span(),
                format!("lowering produced invalid code: {error:?}"),
            ));
            None
        }
    }
}

/// Append an expression's operations in post-order.
fn emit(
    expr: &Expr,
    names: &mut NameTable,
    ops: &mut Vec<Op>,
    diagnostics: &mut Diagnostics,
) -> Option<()> {
    match expr {
        Expr::Literal { value, span } => {
            ops.push(Op::Const(literal_value(value, *span, diagnostics)?))
        }
        Expr::Name(name) => ops.push(Op::Load(names.intern(&name.text))),
        Expr::Unary { op, operand, .. } => {
            emit(operand, names, ops, diagnostics)?;
            ops.push(match op {
                UnaryOp::Neg => Op::Neg,
                UnaryOp::Not => Op::Not,
            });
        }
        Expr::Binary {
            op, left, right, ..
        } => {
            emit(left, names, ops, diagnostics)?;
            emit(right, names, ops, diagnostics)?;
            ops.push(binary_op(*op));
        }
    }
    Some(())
}

fn literal_value(literal: &Literal, span: Span, diagnostics: &mut Diagnostics) -> Option<Value> {
    match literal {
        Literal::Int(value) => Some(Value::Int(*value)),
        Literal::Bool(value) => Some(Value::Bool(*value)),
        Literal::Str(value) => Some(Value::Str(value.clone())),
        // The lexer rejects a non-finite literal, so this cannot fire from
        // source; it is here because `Value::float` is the only constructor
        // that can establish the invariant.
        Literal::Float(value) => Value::float(*value).or_else(|| {
            diagnostics.push(Diagnostic::new(
                Code::NonFiniteFloat,
                span,
                "float literal is not finite",
            ));
            None
        }),
    }
}

const fn binary_op(op: BinaryOp) -> Op {
    match op {
        BinaryOp::Add => Op::Add,
        BinaryOp::Sub => Op::Sub,
        BinaryOp::Mul => Op::Mul,
        BinaryOp::Div => Op::Div,
        BinaryOp::Rem => Op::Rem,
        BinaryOp::Eq => Op::Eq,
        BinaryOp::Ne => Op::Ne,
        BinaryOp::Lt => Op::Lt,
        BinaryOp::Le => Op::Le,
        BinaryOp::Gt => Op::Gt,
        BinaryOp::Ge => Op::Ge,
        BinaryOp::And => Op::And,
        BinaryOp::Or => Op::Or,
    }
}
