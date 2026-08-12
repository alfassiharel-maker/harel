//! What each command does.
//!
//! Every command is: read a file, call a library, format the answer. There is
//! no analysis, no evaluation and no language knowledge in this crate — if a
//! question about the language can be answered here, it is in the wrong place
//! (`docs/05_ARCHITECTURE.md` §2).

use crate::args::{Command, Format};
use lml_ast::print_program;
use lml_compiler::compile;
use lml_diagnostics::Diagnostics;
use lml_ir::print_ir;
use lml_runtime::{run, Execution, Limits, LANGUAGE_VERSION};
use lml_trace::{to_json, why};

/// What a command produced.
pub struct Report {
    /// What to print on standard output.
    pub stdout: String,
    /// What to print on standard error.
    pub stderr: String,
    /// The process exit code.
    pub code: i32,
}

impl Report {
    fn ok(stdout: impl Into<String>) -> Self {
        Self {
            stdout: stdout.into(),
            stderr: String::new(),
            code: 0,
        }
    }

    fn rejected(stderr: impl Into<String>) -> Self {
        Self {
            stdout: String::new(),
            stderr: stderr.into(),
            code: 1,
        }
    }
}

/// Execute a command against the filesystem.
pub fn execute(command: &Command) -> Report {
    match command {
        Command::Help => Report::ok(crate::args::usage()),
        Command::Version => Report::ok(format!(
            "lml {}\nlanguage {LANGUAGE_VERSION}\nir {}\n",
            env!("CARGO_PKG_VERSION"),
            lml_ir::IR_VERSION
        )),
        Command::Check { path } => with_source(path, |source| match compile(source) {
            Ok(_) => Report::ok(format!("{path}: no problems found\n")),
            Err(diagnostics) => rejected(source, path, &diagnostics),
        }),
        Command::Ast { path } => with_source(path, |source| match compile(source) {
            Ok(compilation) => Report::ok(print_program(compilation.program())),
            Err(diagnostics) => rejected(source, path, &diagnostics),
        }),
        Command::Ir { path } => with_source(path, |source| match compile(source) {
            Ok(compilation) => Report::ok(print_ir(&compilation.ir)),
            Err(diagnostics) => rejected(source, path, &diagnostics),
        }),
        Command::Run { path, format } => {
            with_execution(path, |source, execution| match (execution, format) {
                (Ok(execution), Format::Plain) => Report::ok(format!("{}\n", execution.to_text())),
                (Ok(execution), Format::Json) => {
                    Report::ok(format!("{}\n", outputs_json(&execution)))
                }
                (Err(failure), _) => {
                    Report::rejected(format!("{}\n", failure.diagnostic.render(source, path)))
                }
            })
        }
        Command::Trace { path, format } => {
            with_execution(path, |source, execution| {
                // A failed execution still has a trace, and printing it is the
                // whole point of the command.
                let (trace, tail) = match &execution {
                    Ok(execution) => (&execution.trace, String::new()),
                    Err(failure) => (
                        &failure.trace,
                        format!("{}\n", failure.diagnostic.render(source, path)),
                    ),
                };
                let body = match format {
                    Format::Plain => format!("{}\n", trace.to_text()),
                    Format::Json => format!("{}\n", to_json(trace)),
                };
                Report {
                    stdout: body,
                    stderr: tail,
                    code: i32::from(execution.is_err()),
                }
            })
        }
        Command::Why { path, name } => with_execution(path, |source, execution| match execution {
            Ok(execution) => match why(&execution.trace, name) {
                Some(explanation) => Report::ok(explanation.to_text()),
                // The trace does not support an answer, so none is invented.
                None => Report::rejected(format!(
                    "{path}: the trace does not show how `{name}` was derived\n"
                )),
            },
            Err(failure) => {
                Report::rejected(format!("{}\n", failure.diagnostic.render(source, path)))
            }
        }),
    }
}

fn with_source(path: &str, body: impl FnOnce(&str) -> Report) -> Report {
    match std::fs::read_to_string(path) {
        Ok(source) => body(&source),
        Err(error) => Report {
            stdout: String::new(),
            stderr: format!("cannot read {path}: {error}\n"),
            code: 3,
        },
    }
}

type Outcome = Result<Execution, lml_runtime::Failure>;

fn with_execution(path: &str, body: impl FnOnce(&str, Outcome) -> Report) -> Report {
    with_source(path, |source| match compile(source) {
        Ok(compilation) => body(source, run(&compilation.ir, Limits::new())),
        Err(diagnostics) => rejected(source, path, &diagnostics),
    })
}

fn rejected(source: &str, path: &str, diagnostics: &Diagnostics) -> Report {
    Report::rejected(format!("{}\n", diagnostics.render(source, path)))
}

/// The outputs as JSON, with values tagged by type.
fn outputs_json(execution: &Execution) -> String {
    let entries: Vec<String> = execution
        .outputs
        .iter()
        .map(|output| match &output.value {
            lml_trace::Emitted::Known(value) => {
                format!(
                    "{{\"name\":\"{}\",\"value\":{}}}",
                    output.name,
                    json_scalar(value)
                )
            }
            lml_trace::Emitted::Unknown => {
                format!("{{\"name\":\"{}\",\"value\":null}}", output.name)
            }
        })
        .collect();
    format!("{{\"outputs\":[{}]}}", entries.join(","))
}

fn json_scalar(value: &lml_types::Value) -> String {
    match value {
        lml_types::Value::Int(number) => format!("{{\"type\":\"Int\",\"value\":{number}}}"),
        lml_types::Value::Float(number) => format!("{{\"type\":\"Float\",\"value\":{number:?}}}"),
        lml_types::Value::Bool(boolean) => format!("{{\"type\":\"Bool\",\"value\":{boolean}}}"),
        lml_types::Value::Str(text) => {
            format!(
                "{{\"type\":\"String\",\"value\":{}}}",
                lml_ast::print_string(text)
            )
        }
    }
}
