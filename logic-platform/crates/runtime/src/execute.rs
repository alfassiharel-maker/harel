//! The run loop.
//!
//! A pure function of `(IrProgram, Limits)`: no clock, no environment, no I/O,
//! no thread. Everything an execution touches is owned by one
//! [`ExecutionContext`], so two executions in one process share nothing.

use crate::limits::Limits;
use lml_diagnostics::Diagnostic;
use lml_ir::{print_ir, IrProgram};
use lml_logic::{Fact, FactSet, Origin};
use lml_reasoning::infer;
use lml_trace::{sha256_hex, Emitted, Trace, TraceEventKind};

/// The language version this engine implements.
pub const LANGUAGE_VERSION: &str = "0.1";

/// One emitted output.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Output {
    /// The name.
    pub name: String,
    /// Its value, or `Unknown` if nothing derived it. An output that could not
    /// be derived is not an error and is not zero
    /// (`docs/02_FORMAL_SEMANTICS.md` §4.4).
    pub value: Emitted,
}

/// Everything one execution produced.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Execution {
    /// The outputs, in source order.
    pub outputs: Vec<Output>,
    /// Every fact known at the end, in name order.
    pub facts: Vec<(String, Fact)>,
    /// The event log.
    pub trace: Trace,
    /// How many rounds ran.
    pub rounds: usize,
    /// How many rules fired.
    pub rules_fired: usize,
}

impl Execution {
    /// The value emitted for `name`, if it was an output.
    #[must_use]
    pub fn output(&self, name: &str) -> Option<&Emitted> {
        self.outputs
            .iter()
            .find(|output| output.name == name)
            .map(|output| &output.value)
    }

    /// The outputs as `name = value` lines, in source order. The plain-text
    /// result format the CLI prints and the fixtures compare against.
    #[must_use]
    pub fn to_text(&self) -> String {
        self.outputs
            .iter()
            .map(|output| match &output.value {
                Emitted::Known(value) => format!("{} = {value}", output.name),
                Emitted::Unknown => format!("{} = unknown", output.name),
            })
            .collect::<Vec<_>>()
            .join("\n")
    }
}

/// An execution that stopped with an error.
///
/// The trace is kept: a partial trace is how a failure is explained, and
/// throwing it away would leave the most interesting run with no record
/// (`docs/02_FORMAL_SEMANTICS.md` §5).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Failure {
    /// What went wrong.
    pub diagnostic: Diagnostic,
    /// What had happened up to and including the failure.
    pub trace: Trace,
}

/// Everything one execution owns.
///
/// The single owner of runtime state (`docs/03_EXECUTION_MODEL.md` §4). There
/// is no global state anywhere in the workspace: no `static mut`, no singleton,
/// no thread-local.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExecutionContext {
    /// The facts known so far.
    pub facts: FactSet,
    /// The event log.
    pub trace: Trace,
    /// The bounds this execution runs under.
    pub limits: Limits,
}

impl ExecutionContext {
    /// A context with no facts and an empty trace.
    #[must_use]
    pub fn new(limits: Limits) -> Self {
        Self {
            facts: FactSet::new(),
            trace: Trace::new(),
            limits,
        }
    }
}

/// Execute a program.
///
/// # Errors
/// A [`Failure`] carrying the diagnostic and the partial trace.
pub fn run(program: &IrProgram, limits: Limits) -> Result<Execution, Failure> {
    let mut context = ExecutionContext::new(limits);

    context.trace.record(TraceEventKind::ExecutionStarted {
        language_version: LANGUAGE_VERSION.to_owned(),
        ir_version: program.ir_version,
        program_digest: digest(program),
    });

    // Round 0: the declared facts. Their values were folded during lowering, so
    // nothing is evaluated here.
    for fact in &program.facts {
        context
            .facts
            .insert(fact.name, fact.value.clone(), Origin::Declared);
        context.trace.record(TraceEventKind::FactDeclared {
            name: program.name_text(fact.name),
            value: fact.value.clone(),
        });
    }

    let report = match infer(
        program,
        &mut context.facts,
        &mut context.trace,
        limits.max_inference_rounds,
    ) {
        Ok(report) => report,
        Err(diagnostic) => return Err(fail(&mut context.trace, diagnostic)),
    };

    let outputs = program
        .outputs
        .iter()
        .map(|&name| {
            let text = program.name_text(name);
            let value = context
                .facts
                .get(name)
                .map_or(Emitted::Unknown, |value| Emitted::Known(value.clone()));
            context.trace.record(TraceEventKind::OutputEmitted {
                name: text.clone(),
                value: value.clone(),
            });
            Output { name: text, value }
        })
        .collect();

    context.trace.record(TraceEventKind::ExecutionFinished {
        rounds: report.rounds,
        facts_total: context.facts.len(),
        rules_fired: report.rules_fired,
    });

    let facts = context
        .facts
        .iter()
        .map(|(name, fact)| (program.name_text(name), fact.clone()))
        .collect();

    Ok(Execution {
        outputs,
        facts,
        trace: context.trace,
        rounds: report.rounds,
        rules_fired: report.rules_fired,
    })
}

/// The program's identity: SHA-256 of its canonical IR text.
///
/// Recorded in the trace so that a trace can be checked against the program it
/// claims to describe.
#[must_use]
pub fn digest(program: &IrProgram) -> String {
    sha256_hex(print_ir(program).as_bytes())
}

fn fail(trace: &mut Trace, diagnostic: Diagnostic) -> Failure {
    trace.record(TraceEventKind::ErrorRaised {
        code: diagnostic.code.to_string(),
        message: diagnostic.message.clone(),
    });
    Failure {
        diagnostic,
        trace: trace.clone(),
    }
}
