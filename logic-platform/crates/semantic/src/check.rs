//! The static checks of `docs/02_FORMAL_SEMANTICS.md` §3.
//!
//! Every check runs on every program: analysis never stops at the first error,
//! because a developer fixing a file wants the whole list. What analysis does
//! *not* do is report consequences of an error it already reported — a name
//! whose type could not be inferred is silently untyped from then on, so one
//! mistake yields one diagnostic.

use crate::model::{NameInfo, RuleInfo, SemanticModel, Writer};
use lml_ast::{Expr, Name, Program};
use lml_diagnostics::{Code, Diagnostic, Diagnostics, Span};
use lml_types::{binary_result, type_of_literal, unary_result, Type};
use std::collections::{BTreeMap, BTreeSet};

/// Longest chain of names whose types depend on one another.
///
/// Type inference recurses through name definitions, so a chain of 100_000
/// names would overflow the stack. The limit turns that into `E8004`. It is far
/// above any real program: a chain this long is generated, not written.
pub const MAX_TYPE_DEPENDENCY_DEPTH: usize = 512;

/// Validate a program.
///
/// # Errors
/// Returns every static diagnostic, in source order.
pub fn analyze(program: Program) -> Result<SemanticModel, Diagnostics> {
    let mut diagnostics = Diagnostics::new();

    let definitions = collect_definitions(&program, &mut diagnostics);
    check_reads_are_writable(&program, &definitions, &mut diagnostics);
    check_facts_are_constant(&program, &definitions, &mut diagnostics);
    let outputs = collect_outputs(&program, &definitions, &mut diagnostics);

    let types = infer_types(&program, &definitions, &mut diagnostics);

    diagnostics.sort_by_position();
    if !diagnostics.is_empty() {
        return Err(diagnostics);
    }

    let mut names = BTreeMap::new();
    for (name, definition) in &definitions {
        // Unreachable unless inference reported an error, in which case we
        // returned above.
        let Some(Some(ty)) = types.get(name) else {
            continue;
        };
        names.insert(
            name.clone(),
            NameInfo {
                ty: *ty,
                writers: definition.writers.clone(),
            },
        );
    }

    let rules = program
        .rules()
        .map(|rule| RuleInfo {
            name: rule.name.text.clone(),
            reads: reads_of_rule(rule),
            writes: rule
                .effects
                .iter()
                .map(|effect| effect.name.text.clone())
                .collect(),
            span: rule.span,
        })
        .collect();

    Ok(SemanticModel {
        program,
        names,
        rules,
        outputs,
    })
}

/// Everything that can bind one name.
struct Definition<'a> {
    writers: Vec<Writer>,
    /// The defining expressions, paired with the span to blame for a type
    /// error, in the same order as `writers`.
    values: Vec<(&'a Expr, Span)>,
}

type Definitions<'a> = BTreeMap<String, Definition<'a>>;

/// N1, N2, N4 — collect the writers of every name, reporting duplicates.
fn collect_definitions<'a>(program: &'a Program, diagnostics: &mut Diagnostics) -> Definitions<'a> {
    let mut definitions: Definitions<'a> = BTreeMap::new();
    let mut declared: BTreeMap<&str, Span> = BTreeMap::new();

    for fact in program.facts() {
        if let Some(&first) = declared.get(fact.name.text.as_str()) {
            diagnostics.push(
                Diagnostic::new(
                    Code::DuplicateFact,
                    fact.name.span,
                    format!("`{}` is declared more than once", fact.name),
                )
                .with_label(first, "first declared here")
                .with_help("a fact is bound at most once; facts are immutable"),
            );
            continue;
        }
        declared.insert(&fact.name.text, fact.name.span);
        definitions.insert(
            fact.name.text.clone(),
            Definition {
                writers: vec![Writer::Declaration { span: fact.span }],
                values: vec![(&fact.value, fact.value.span())],
            },
        );
    }

    let mut rule_names: BTreeMap<&str, Span> = BTreeMap::new();
    for (index, rule) in program.rules().enumerate() {
        if let Some(&first) = rule_names.get(rule.name.text.as_str()) {
            diagnostics.push(
                Diagnostic::new(
                    Code::DuplicateRule,
                    rule.name.span,
                    format!("`{}` is the name of more than one rule", rule.name),
                )
                .with_label(first, "first defined here")
                .with_help("rule names identify a rule in traces and conflict reports"),
            );
        } else {
            rule_names.insert(&rule.name.text, rule.name.span);
        }

        for effect in &rule.effects {
            if let Some(&first) = declared.get(effect.name.text.as_str()) {
                diagnostics.push(
                    Diagnostic::new(
                        Code::DerivationShadowsFact,
                        effect.name.span,
                        format!(
                            "rule `{}` derives `{}`, which is already a fact",
                            rule.name, effect.name
                        ),
                    )
                    .with_label(first, "declared here")
                    .with_help("facts are immutable, so this could only ever conflict"),
                );
                continue;
            }
            definitions
                .entry(effect.name.text.clone())
                .or_insert_with(|| Definition {
                    writers: Vec::new(),
                    values: Vec::new(),
                })
                .push(
                    Writer::Rule {
                        index,
                        span: effect.span,
                    },
                    &effect.value,
                    effect.value.span(),
                );
        }
    }

    definitions
}

impl<'a> Definition<'a> {
    fn push(&mut self, writer: Writer, value: &'a Expr, span: Span) {
        self.writers.push(writer);
        self.values.push((value, span));
    }
}

/// N3 — every name that is read must be writable by something.
fn check_reads_are_writable(
    program: &Program,
    definitions: &Definitions<'_>,
    diagnostics: &mut Diagnostics,
) {
    let mut report = |name: &Name| {
        if !definitions.contains_key(&name.text) {
            diagnostics.push(
                Diagnostic::new(
                    Code::UnknownName,
                    name.span,
                    format!("nothing in this program can give `{name}` a value"),
                )
                .with_help("declare it with `fact`, or derive it in a rule's `then` clause"),
            );
        }
    };

    for fact in program.facts() {
        fact.value.visit_names(&mut report);
    }
    for rule in program.rules() {
        rule.condition.visit_names(&mut report);
        for effect in &rule.effects {
            effect.value.visit_names(&mut report);
        }
    }
}

/// N7 — a `fact`'s value is a constant expression.
///
/// A fact is *given*, not computed: `fact b = a + 1` would need an evaluation
/// order for round 0 that the semantics do not define, and the thing it is
/// reaching for — a value derived from another value — is exactly what a rule
/// is. Forbidding it keeps round 0 a set of constants and keeps the fixed point
/// the only place where derivation happens.
fn check_facts_are_constant(
    program: &Program,
    definitions: &Definitions<'_>,
    diagnostics: &mut Diagnostics,
) {
    for fact in program.facts() {
        let mut report = |name: &Name| {
            // A name nothing can write is already reported as `E3003`; saying
            // it twice for one mistake helps nobody.
            if !definitions.contains_key(&name.text) {
                return;
            }
            diagnostics.push(
                Diagnostic::new(
                    Code::NonConstantFact,
                    name.span,
                    format!("the value of fact `{}` reads `{name}`", fact.name),
                )
                .with_help("a fact's value must be a constant; use a rule to derive one value from another"),
            );
        };
        fact.value.visit_names(&mut report);
    }
}

/// N5, N6 — outputs name something writable, and name it once.
fn collect_outputs(
    program: &Program,
    definitions: &Definitions<'_>,
    diagnostics: &mut Diagnostics,
) -> Vec<String> {
    let mut outputs = Vec::new();
    let mut seen: BTreeMap<&str, Span> = BTreeMap::new();

    for declaration in program.outputs() {
        for name in &declaration.names {
            if !definitions.contains_key(&name.text) {
                diagnostics.push(
                    Diagnostic::new(
                        Code::UnknownName,
                        name.span,
                        format!("nothing in this program can give `{name}` a value"),
                    )
                    .with_help("an output must name a fact or something a rule derives"),
                );
                continue;
            }
            if let Some(&first) = seen.get(name.text.as_str()) {
                diagnostics.push(
                    Diagnostic::new(
                        Code::DuplicateOutput,
                        name.span,
                        format!("`{name}` is output more than once"),
                    )
                    .with_label(first, "first output here"),
                );
                continue;
            }
            seen.insert(&name.text, name.span);
            outputs.push(name.text.clone());
        }
    }

    outputs
}

/// The names a rule reads, condition and consequences alike.
fn reads_of_rule(rule: &lml_ast::RuleDecl) -> BTreeSet<String> {
    let mut reads = BTreeSet::new();
    let mut collect = |name: &Name| {
        reads.insert(name.text.clone());
    };
    rule.condition.visit_names(&mut collect);
    for effect in &rule.effects {
        effect.value.visit_names(&mut collect);
    }
    reads
}

/// The result of inference: `None` for a name whose type could not be
/// determined, which means a diagnostic was already reported for it.
type Types = BTreeMap<String, Option<Type>>;

/// T1–T5 — give every name exactly one type, and check every condition.
///
/// Conditions are typed here rather than in a second pass because their
/// operator errors are found by the same walk, and doing it twice would report
/// each of them twice.
fn infer_types(
    program: &Program,
    definitions: &Definitions<'_>,
    diagnostics: &mut Diagnostics,
) -> Types {
    let mut typer = Typer {
        definitions,
        types: BTreeMap::new(),
        visiting: Vec::new(),
        diagnostics,
    };
    for name in definitions.keys() {
        typer.name_type(name, None);
    }

    // T2 — a rule's condition is `Bool`. There is no truthiness.
    for rule in program.rules() {
        match typer.expr_type(&rule.condition) {
            // `None` means the walk already reported why.
            Some(Type::Bool) | None => {}
            Some(other) => typer.diagnostics.push(
                Diagnostic::new(
                    Code::NonBooleanCondition,
                    rule.condition.span(),
                    format!(
                        "the condition of rule `{}` is `{other}`, not `Bool`",
                        rule.name
                    ),
                )
                .with_help("a condition must be a comparison or a boolean expression"),
            ),
        }
    }

    typer.types
}

struct Typer<'a, 'src> {
    definitions: &'a Definitions<'src>,
    types: Types,
    visiting: Vec<String>,
    diagnostics: &'a mut Diagnostics,
}

impl Typer<'_, '_> {
    /// The type of `name`, inferring it if this is the first request.
    ///
    /// `used_at` is where the name was read, used to place a diagnostic when
    /// the name itself has no source position to blame.
    fn name_type(&mut self, name: &str, used_at: Option<Span>) -> Option<Type> {
        if let Some(known) = self.types.get(name) {
            return *known;
        }
        let Some(definition) = self.definitions.get(name) else {
            return None; // reported by check_reads_are_writable
        };

        if self.visiting.iter().any(|visited| visited == name) {
            let chain = self.visiting.join(" → ");
            let span = definition.values.first().map_or_else(
                || used_at.unwrap_or_else(|| Span::point(0)),
                |&(_, span)| span,
            );
            self.diagnostics.push(
                Diagnostic::new(
                    Code::CyclicTypeDependency,
                    span,
                    format!("the type of `{name}` depends on itself, through {chain} → {name}"),
                )
                .with_help(
                    "a value may depend on itself; a type may not — give one of these a literal",
                ),
            );
            self.types.insert(name.to_owned(), None);
            return None;
        }

        if self.visiting.len() >= MAX_TYPE_DEPENDENCY_DEPTH {
            let span = definition.values.first().map_or_else(
                || used_at.unwrap_or_else(|| Span::point(0)),
                |&(_, span)| span,
            );
            self.diagnostics.push(Diagnostic::new(
                Code::TypeDependencyTooDeep,
                span,
                format!("names depend on one another more than {MAX_TYPE_DEPENDENCY_DEPTH} deep"),
            ));
            self.types.insert(name.to_owned(), None);
            return None;
        }

        self.visiting.push(name.to_owned());
        let mut result: Option<Type> = None;
        let mut first: Option<(Type, Span)> = None;
        let mut failed = false;
        for &(value, span) in &definition.values {
            let Some(ty) = self.expr_type(value) else {
                failed = true;
                continue;
            };
            match first {
                None => {
                    first = Some((ty, span));
                    result = Some(ty);
                }
                Some((expected, first_span)) if expected != ty => {
                    // T1 — several rules deriving one name must agree.
                    self.diagnostics.push(
                        Diagnostic::new(
                            Code::ConflictingNameType,
                            span,
                            format!(
                                "`{name}` is derived as `{ty}` here and as `{expected}` elsewhere"
                            ),
                        )
                        .with_label(first_span, format!("derived as `{expected}` here"))
                        .with_help("every writer of a name must produce the same type"),
                    );
                    failed = true;
                }
                Some(_) => {}
            }
        }
        self.visiting.pop();

        let resolved = if failed { None } else { result };
        self.types.insert(name.to_owned(), resolved);
        resolved
    }

    /// The type of an expression, or `None` once something has been reported.
    fn expr_type(&mut self, expr: &Expr) -> Option<Type> {
        match expr {
            Expr::Literal { value, .. } => Some(type_of_literal(value)),
            Expr::Name(name) => self.name_type(&name.text, Some(name.span)),
            Expr::Unary { op, operand, span } => {
                let operand_type = self.expr_type(operand)?;
                unary_result(*op, operand_type).or_else(|| {
                    self.diagnostics.push(Diagnostic::new(
                        Code::OperatorTypeMismatch,
                        *span,
                        format!("`{}` is not defined for `{operand_type}`", op.symbol()),
                    ));
                    None
                })
            }
            Expr::Binary {
                op,
                left,
                right,
                span,
            } => {
                // Both sides are typed before the result is judged, so a
                // program with two type errors reports two diagnostics.
                let left_type = self.expr_type(left);
                let right_type = self.expr_type(right);
                let (left_type, right_type) = (left_type?, right_type?);
                binary_result(*op, left_type, right_type).or_else(|| {
                    let help = if left_type != right_type {
                        "there is no implicit conversion between types; both sides must already agree"
                    } else {
                        "this operator is not defined for that type"
                    };
                    self.diagnostics.push(
                        Diagnostic::new(
                            Code::OperatorTypeMismatch,
                            *span,
                            format!(
                                "`{}` is not defined for `{left_type}` and `{right_type}`",
                                op.symbol()
                            ),
                        )
                        .with_help(help),
                    );
                    None
                })
            }
        }
    }
}
