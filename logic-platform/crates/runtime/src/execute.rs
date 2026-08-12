//! The run loop.
//!
//! Semantically a pure function of `(IrProgram, Limits)`: no environment, no
//! I/O, no thread, and nothing that varies between runs takes part in producing
//! a result or an event. Everything an execution touches is owned by one
//! [`ExecutionContext`], so two executions in one process share nothing.
//!
//! The one thing that *is* read from the outside is the clock, and only to
//! stamp [`lml_trace::TraceMetadata`] — an execution id and a duration, which
//! Master Spec §10 requires and which trace equality excludes. No event, no
//! fact and no output can observe it.

use crate::limits::Limits;
use lml_diagnostics::Diagnostic;
use lml_ir::{print_ir, IrProgram};
use lml_logic::{Fact, FactSet, Origin};
use lml_reasoning::infer;
use lml_trace::{sha256_hex, Trace, TraceEventKind, TraceMetadata};
use lml_types::Value;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

/// The language version this engine implements.
pub const LANGUAGE_VERSION: &str = "0.1";

/// One emitted output.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Output {
    /// The name.
    pub name: String,
    /// Its value, in one of the three states.
    ///
    /// `Unknown` means nothing derived it — not an error, and not zero. `Null`
    /// means something derived it as a *known absence*, which is a different
    /// answer (owner decision OD-2).
    pub value: Value,
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
    /// How many rules were still pending at the fixed point.
    pub rules_pending: usize,
}

impl Execution {
    /// The value emitted for `name`, if it was an output.
    #[must_use]
    pub fn output(&self, name: &str) -> Option<&Value> {
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
            .map(|output| format!("{} = {}", output.name, output.value))
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
// The error variant is large because it carries the trace, deliberately: a
// failed execution's trace is how the failure is explained, and boxing it would
// add an indirection to every caller to save an allocation on a path that ends
// the run. The size is the design, not an oversight.
#[allow(clippy::result_large_err)]
pub fn run(program: &IrProgram, limits: Limits) -> Result<Execution, Failure> {
    let mut context = ExecutionContext::new(limits);
    let digest = digest(program);
    // Wall-clock data is metadata, never semantics: it is attached to the trace
    // at the end and excluded from trace equality (owner instruction §10).
    let started = Instant::now();
    let started_at_unix_nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |since| since.as_nanos());

    context.trace.record(TraceEventKind::ExecutionStarted {
        language_version: LANGUAGE_VERSION.to_owned(),
        ir_version: program.ir_version,
        program_digest: digest.clone(),
    });

    // Round 0: the declared facts. Their values were folded during lowering, so
    // nothing is evaluated here.
    for fact in &program.facts {
        // The trace event that records the binding is the provenance a conflict
        // report points at, so its seq is reserved before the insert.
        let seq = context.trace.next_seq();
        context
            .facts
            .insert(fact.name, fact.value.clone(), Origin::Declared, seq);
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
        Err(diagnostic) => {
            let mut failure = fail(&mut context.trace, diagnostic);
            // A failed run is measured too: the metadata is attached to the
            // partial trace exactly as it would have been to a complete one.
            stamp(&mut failure.trace, &digest, started_at_unix_nanos, started);
            return Err(failure);
        }
    };

    let outputs = program
        .outputs
        .iter()
        .map(|&name| {
            let text = program.name_text(name);
            // Absence from the fact set is `Unknown`; a stored `Null` is a
            // known absence. The two reach the output distinct.
            let value = context.facts.get(name).cloned().unwrap_or(Value::Unknown);
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
        rules_pending: report.rules_pending,
    });
    stamp(&mut context.trace, &digest, started_at_unix_nanos, started);

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
        rules_pending: report.rules_pending,
    })
}

/// Attach the run's wall-clock metadata.
///
/// The execution id is the program digest plus the start instant: it identifies
/// *this run of this program*, which is what Master Spec §10 asks for. It lives
/// in metadata precisely because it differs between runs, and trace equality
/// must not.
fn stamp(trace: &mut Trace, digest: &str, started_at_unix_nanos: u128, started: Instant) {
    let short: String = digest.chars().take(12).collect();
    trace.set_metadata(TraceMetadata {
        execution_id: format!("{short}-{started_at_unix_nanos}"),
        started_at_unix_nanos,
        duration_nanos: started.elapsed().as_nanos(),
    });
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
        cause: diagnostic.cause.clone(),
    });
    Failure {
        diagnostic,
        trace: trace.clone(),
    }
}
