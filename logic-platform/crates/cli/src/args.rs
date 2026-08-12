//! Argument parsing.
//!
//! Hand-written: the surface is seven commands and two flags, and a parser for
//! that is 100 lines. Taking a dependency for it would be the first
//! third-party crate in the workspace, which needs the justification of
//! `docs/ENGINEERING_IMPLEMENTATION_SPEC.md` §30 — and "it would save an hour"
//! is not one.

/// What the user asked for.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Command {
    /// Compile and report diagnostics, without executing.
    Check {
        /// The source file.
        path: String,
    },
    /// Execute and print the outputs.
    Run {
        /// The source file.
        path: String,
        /// How to print the result.
        format: Format,
    },
    /// Execute and print the trace.
    Trace {
        /// The source file.
        path: String,
        /// How to print the trace.
        format: Format,
    },
    /// Print the canonical syntax tree.
    Ast {
        /// The source file.
        path: String,
    },
    /// Print the canonical IR.
    Ir {
        /// The source file.
        path: String,
    },
    /// Explain one name from the execution's trace.
    Why {
        /// The source file.
        path: String,
        /// The name to explain.
        name: String,
    },
    /// Print the versions.
    Version,
    /// Print usage.
    Help,
}

/// Output format.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Format {
    /// Human-readable text.
    Plain,
    /// Machine-readable JSON.
    Json,
}

/// Why the arguments could not be understood.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UsageError(pub String);

/// Parse the arguments after the program name.
///
/// # Errors
/// A message naming what was wrong, which `main` prints with the usage text.
pub fn parse(args: &[String]) -> Result<Command, UsageError> {
    let Some(command) = args.first() else {
        return Ok(Command::Help);
    };

    match command.as_str() {
        "help" | "--help" | "-h" => Ok(Command::Help),
        "version" | "--version" | "-V" => Ok(Command::Version),
        "check" => Ok(Command::Check {
            path: path_argument(args, "check")?,
        }),
        "ast" => Ok(Command::Ast {
            path: path_argument(args, "ast")?,
        }),
        "ir" => Ok(Command::Ir {
            path: path_argument(args, "ir")?,
        }),
        "run" => Ok(Command::Run {
            path: path_argument(args, "run")?,
            format: format_flag(args, Format::Plain)?,
        }),
        "trace" => Ok(Command::Trace {
            path: path_argument(args, "trace")?,
            format: format_flag(args, Format::Plain)?,
        }),
        "why" => {
            let path = path_argument(args, "why")?;
            let name = args
                .get(2)
                .filter(|argument| !argument.starts_with("--"))
                .ok_or_else(|| UsageError("`why` needs a file and a name".to_owned()))?;
            Ok(Command::Why {
                path,
                name: name.clone(),
            })
        }
        other => Err(UsageError(format!("`{other}` is not a command"))),
    }
}

fn path_argument(args: &[String], command: &str) -> Result<String, UsageError> {
    args.get(1)
        .filter(|argument| !argument.starts_with("--"))
        .cloned()
        .ok_or_else(|| UsageError(format!("`{command}` needs a file")))
}

fn format_flag(args: &[String], default: Format) -> Result<Format, UsageError> {
    for argument in args {
        let Some(value) = argument.strip_prefix("--format=") else {
            continue;
        };
        return match value {
            "plain" | "text" => Ok(Format::Plain),
            "json" => Ok(Format::Json),
            other => Err(UsageError(format!(
                "`{other}` is not a format; use plain or json"
            ))),
        };
    }
    Ok(default)
}

/// The usage text.
#[must_use]
pub fn usage() -> String {
    "\
lml — the LML language tool

USAGE
    lml <command> [arguments]

COMMANDS
    check <file>              compile and report diagnostics
    run <file>                execute and print the outputs
    trace <file>              execute and print the reasoning trace
    why <file> <name>         explain one result from the trace
    ast <file>                print the canonical syntax tree
    ir <file>                 print the canonical intermediate representation
    version                   print the tool, language and IR versions
    help                      print this text

FLAGS
    --format=plain|json       for `run` and `trace` (default: plain)

EXIT CODES
    0  success
    1  the program was rejected, or execution failed
    2  the command line was not understood
    3  a file could not be read
"
    .to_owned()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(items: &[&str]) -> Vec<String> {
        items.iter().map(|item| (*item).to_owned()).collect()
    }

    #[test]
    fn commands_parse() {
        assert_eq!(
            parse(&args(&["check", "a.lml"])),
            Ok(Command::Check {
                path: "a.lml".into()
            })
        );
        assert_eq!(
            parse(&args(&["run", "a.lml", "--format=json"])),
            Ok(Command::Run {
                path: "a.lml".into(),
                format: Format::Json
            })
        );
        assert_eq!(
            parse(&args(&["why", "a.lml", "status"])),
            Ok(Command::Why {
                path: "a.lml".into(),
                name: "status".into()
            })
        );
        assert_eq!(parse(&args(&[])), Ok(Command::Help));
    }

    #[test]
    fn mistakes_are_reported_not_guessed() {
        assert!(parse(&args(&["check"])).is_err());
        assert!(parse(&args(&["run", "a.lml", "--format=yaml"])).is_err());
        assert!(parse(&args(&["frobnicate", "a.lml"])).is_err());
        assert!(parse(&args(&["why", "a.lml"])).is_err());
    }
}
