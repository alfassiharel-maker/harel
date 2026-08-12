//! The `lml` command line.
//!
//! A thin entry point: parse the arguments, run the command, print the result,
//! choose an exit code. Everything else lives in a library
//! (`docs/ENGINEERING_IMPLEMENTATION_SPEC.md` §32).

#![forbid(unsafe_code)]
#![warn(missing_docs)]

mod args;
mod run;

use std::io::Write;
use std::process::ExitCode;

fn main() -> ExitCode {
    let arguments: Vec<String> = std::env::args().skip(1).collect();

    let report = match args::parse(&arguments) {
        Ok(command) => run::execute(&command),
        Err(args::UsageError(message)) => run::Report {
            stdout: String::new(),
            stderr: format!("{message}\n\n{}", args::usage()),
            code: 2,
        },
    };

    print(&report.stdout, &mut std::io::stdout());
    print(&report.stderr, &mut std::io::stderr());

    // Exit codes are documented in `args::usage`. They are the CLI's contract
    // with a build script, so they are as much an interface as the output is.
    ExitCode::from(u8::try_from(report.code).unwrap_or(1))
}

/// Write, ignoring a closed pipe.
///
/// `lml trace big.lml | head` closes the pipe early; that is the user getting
/// what they asked for, not a failure worth reporting.
fn print(text: &str, out: &mut impl Write) {
    if text.is_empty() {
        return;
    }
    let _ = out.write_all(text.as_bytes());
    let _ = out.flush();
}
