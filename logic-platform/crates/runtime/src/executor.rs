//! Execution context: load → infer → query.

use ast::Program;
use diagnostics::DiagnosticBag;
use logic::{RelName, Term};
use reasoning::inference::{evaluate, query};
use thiserror::Error;
use trace::TraceLog;

use crate::loader::load;
use crate::output::{QueryAnswer, QueryRow};

/// Errors that can occur during program execution.
#[derive(Debug, Error)]
pub enum ExecutionError {
    #[error("program has parse or semantic errors")]
    DiagnosticErrors(Vec<diagnostics::Diagnostic>),
    #[error(transparent)]
    Conflict(#[from] reasoning::conflict::ConflictError),
}

/// Owns the execution state for one program run.
pub struct ExecutionContext {
    pub store: logic::FactStore,
    pub trace: TraceLog,
}

impl ExecutionContext {
    /// Load and execute a program: assert base facts, run inference, return context.
    pub fn run(program: &Program) -> Result<Self, ExecutionError> {
        let mut bag = DiagnosticBag::new();
        let mut trace = TraceLog::new();

        let mut loaded = load(program, &mut bag, &mut trace);

        if bag.has_errors() {
            return Err(ExecutionError::DiagnosticErrors(
                bag.into_diagnostics(),
            ));
        }

        evaluate(&mut loaded.store, &loaded.rules, &mut trace)?;

        Ok(ExecutionContext {
            store: loaded.store,
            trace,
        })
    }

    /// Answer all query declarations in the program.
    pub fn answer_queries(&mut self, program: &Program) -> Vec<QueryAnswer> {
        let mut answers = Vec::new();

        for query_decl in program.queries() {
            let rel = RelName(query_decl.atom.relation.clone());

            // Build query terms: variables become open slots (VarId = position index).
            let query_terms: Vec<Term> = query_decl
                .atom
                .args
                .iter()
                .enumerate()
                .map(|(i, term)| {
                    if term.is_variable() {
                        Term::Variable(logic::VarId(i as u32))
                    } else {
                        // Constants in queries constrain the match.
                        match term {
                            ast::TermNode::ConstStr { value, .. } => {
                                Term::Constant(types::Value::Known(types::KnownValue::Str(value.clone())))
                            }
                            ast::TermNode::ConstInt { value, .. } => {
                                Term::Constant(types::Value::Known(types::KnownValue::Int(*value)))
                            }
                            ast::TermNode::ConstBool { value, .. } => {
                                Term::Constant(types::Value::Known(types::KnownValue::Bool(*value)))
                            }
                            ast::TermNode::Null { .. } => Term::Constant(types::Value::Null),
                            ast::TermNode::Unknown { .. } => Term::Constant(types::Value::Unknown),
                            _ => Term::Variable(logic::VarId(i as u32)),
                        }
                    }
                })
                .collect();

            let facts = query(&rel, &query_terms, &self.store, &mut self.trace);
            let rows: Vec<QueryRow> = facts.iter().map(QueryRow::from_fact).collect();
            answers.push(QueryAnswer {
                relation: rel,
                results: rows,
            });
        }

        answers
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use parser::parse_source;
    use diagnostics::DiagnosticBag;

    fn run(src: &str) -> (ExecutionContext, ast::Program) {
        let mut bag = DiagnosticBag::new();
        let program = parse_source(src, "<test>", &mut bag);
        assert!(!bag.has_errors(), "parse errors: {:?}", bag.diagnostics());
        let ctx = ExecutionContext::run(&program).expect("execution failed");
        (ctx, program)
    }

    #[test]
    fn grandparent_end_to_end() {
        let src = r#"
            fact parent("Alice", "Bob");
            fact parent("Bob", "Charlie");
            rule grandparent(X, Z) when parent(X, Y), parent(Y, Z);
            query grandparent(X, Y);
        "#;
        let (mut ctx, program) = run(src);
        let answers = ctx.answer_queries(&program);
        assert_eq!(answers.len(), 1);
        assert_eq!(answers[0].results.len(), 1);
        assert_eq!(
            answers[0].results[0].args,
            vec![
                types::Value::Known(types::KnownValue::Str("Alice".into())),
                types::Value::Known(types::KnownValue::Str("Charlie".into())),
            ]
        );
        // Provenance must show derivation.
        assert!(matches!(
            answers[0].results[0].provenance,
            trace::ProvenanceSummary::Derived { .. }
        ));
    }

    #[test]
    fn query_with_no_results_returns_empty() {
        let src = r#"
            fact a("x");
            query b(X);
        "#;
        let (mut ctx, program) = run(src);
        let answers = ctx.answer_queries(&program);
        assert_eq!(answers[0].results.len(), 0);
    }

    #[test]
    fn three_state_values_survive_loading() {
        let src = r#"
            fact status(null, unknown);
            query status(X, Y);
        "#;
        let (mut ctx, program) = run(src);
        let answers = ctx.answer_queries(&program);
        assert_eq!(answers[0].results.len(), 1);
        assert_eq!(answers[0].results[0].args[0], types::Value::Null);
        assert_eq!(answers[0].results[0].args[1], types::Value::Unknown);
    }
}
