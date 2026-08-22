//! AST → logic model translation (semantic lowering).
//!
//! Validates and lowers `ast::Program` into the logic layer's `FactStore` and
//! `Vec<Rule>`.  Semantic errors (e.g. unbound head variables) are collected in
//! a `DiagnosticBag`.

use std::collections::HashMap;

use ast::{Decl, FactDecl, Program, RuleDecl, TermNode};
use diagnostics::{DiagnosticBag, SEMANTIC_UNBOUND_VAR_IN_HEAD};
use logic::{Atom, FactStore, GroundFact, Provenance, RelName, Rule, Term, VarId};
use trace::{TraceEventKind, TraceLog};
use types::{KnownValue, Value};

/// Result of loading a program.
pub struct LoadedProgram {
    pub store: FactStore,
    pub rules: Vec<Rule>,
}

/// Lower an `ast::Program` into the logic model.
pub fn load(program: &Program, bag: &mut DiagnosticBag, trace: &mut TraceLog) -> LoadedProgram {
    trace.push(TraceEventKind::ExecutionStarted {
        source_name: program.source_name.clone(),
    });

    let mut store = FactStore::new();
    let mut rules = Vec::new();

    for decl in &program.decls {
        match decl {
            Decl::Fact(fact_decl) => {
                if let Some(fact) = lower_fact(fact_decl, bag) {
                    trace.push(TraceEventKind::FactDeclared {
                        relation: fact.relation.clone(),
                        args: fact.args.clone(),
                    });
                    store.insert(fact);
                }
            }
            Decl::Rule(rule_decl) => {
                if let Some(rule) = lower_rule(rule_decl, bag) {
                    rules.push(rule);
                }
            }
            Decl::Query(_) => {}
        }
    }

    LoadedProgram { store, rules }
}

/// Lower a fact declaration into a `GroundFact`.
fn lower_fact(decl: &FactDecl, bag: &mut DiagnosticBag) -> Option<GroundFact> {
    let relation = RelName(decl.atom.relation.clone());
    let mut args = Vec::new();

    for term in &decl.atom.args {
        match lower_ground_term(term) {
            Some(val) => args.push(val),
            None => {
                bag.error(
                    SEMANTIC_UNBOUND_VAR_IN_HEAD,
                    "fact arguments must be ground (no variables allowed in facts)",
                    Some(term.span().clone()),
                );
                return None;
            }
        }
    }

    Some(GroundFact {
        relation,
        args,
        provenance: Provenance::Asserted,
    })
}

/// Lower a rule declaration into a `Rule`.
fn lower_rule(decl: &RuleDecl, bag: &mut DiagnosticBag) -> Option<Rule> {
    let mut var_map: HashMap<String, VarId> = HashMap::new();
    let mut next_var: u32 = 0;

    let mut get_var = |name: &str| -> VarId {
        if let Some(id) = var_map.get(name) {
            *id
        } else {
            let id = VarId(next_var);
            var_map.insert(name.to_string(), id);
            next_var += 1;
            id
        }
    };

    // Lower body first so all variables are registered before we check the head.
    let mut body = Vec::new();
    for atom_node in &decl.body {
        let atom = lower_atom(atom_node, &mut get_var);
        body.push(atom);
    }

    let head = lower_atom(&decl.head, &mut get_var);

    // Validate: every variable in the head must appear in the body (range restriction).
    for (i, term) in head.args.iter().enumerate() {
        if let Term::Variable(var) = term {
            let in_body = body.iter().any(|atom| {
                atom.args.iter().any(|t| matches!(t, Term::Variable(v) if v == var))
            });
            if !in_body {
                // Find the variable name for a useful error message.
                let name = var_map.iter().find(|(_, id)| **id == *var).map(|(n, _)| n.as_str()).unwrap_or("?");
                bag.error(
                    SEMANTIC_UNBOUND_VAR_IN_HEAD,
                    format!("variable {name} in rule head is not bound in any body predicate"),
                    Some(decl.head.args[i].span().clone()),
                );
            }
        }
    }

    Some(Rule {
        head,
        body,
        var_count: next_var,
    })
}

fn lower_atom(node: &ast::AtomNode, get_var: &mut impl FnMut(&str) -> VarId) -> Atom {
    let args = node.args.iter().map(|t| lower_term(t, get_var)).collect();
    Atom {
        relation: RelName(node.relation.clone()),
        args,
    }
}

fn lower_term(node: &TermNode, get_var: &mut impl FnMut(&str) -> VarId) -> Term {
    match node {
        TermNode::Variable { name, .. } => Term::Variable(get_var(name)),
        other => Term::Constant(lower_ground_term(other).unwrap_or(Value::Unknown)),
    }
}

fn lower_ground_term(node: &TermNode) -> Option<Value> {
    match node {
        TermNode::Variable { .. } => None,
        TermNode::ConstStr { value, .. } => Some(Value::Known(KnownValue::Str(value.clone()))),
        TermNode::ConstInt { value, .. } => Some(Value::Known(KnownValue::Int(*value))),
        TermNode::ConstFloat { value, .. } => Some(Value::Known(KnownValue::Float(*value))),
        TermNode::ConstBool { value, .. } => Some(Value::Known(KnownValue::Bool(*value))),
        TermNode::Null { .. } => Some(Value::Null),
        TermNode::Unknown { .. } => Some(Value::Unknown),
    }
}
