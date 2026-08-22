//! lgc — the Logic language CLI.
//!
//! Usage:
//!   lgc run <file>           run a program and print query results
//!   lgc run <file> --trace   also print the execution trace (NDJSON)
//!   lgc check <file>         parse and validate without running

use std::process;

fn main() {
    let args: Vec<String> = std::env::args().collect();

    if args.len() < 3 {
        eprintln!("usage: lgc <run|check> <file> [--trace]");
        process::exit(2);
    }

    let subcommand = &args[1];
    let file_path = &args[2];
    let show_trace = args.iter().any(|a| a == "--trace");

    let source = match std::fs::read_to_string(file_path) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("error: cannot read {file_path}: {e}");
            process::exit(2);
        }
    };

    let mut bag = diagnostics::DiagnosticBag::new();
    let program = parser::parse_source(&source, file_path, &mut bag);

    for diag in bag.diagnostics() {
        eprintln!("{diag}");
    }

    if bag.has_errors() {
        process::exit(1);
    }

    match subcommand.as_str() {
        "check" => {
            // Parse-and-validate only; no execution.
            println!("ok");
        }

        "run" => {
            let mut ctx = match runtime::ExecutionContext::run(&program) {
                Ok(ctx) => ctx,
                Err(e) => {
                    eprintln!("error: {e}");
                    process::exit(1);
                }
            };

            let answers = ctx.answer_queries(&program);
            for answer in &answers {
                println!("{}", answer.display());
            }

            if show_trace {
                eprintln!("--- trace ---");
                eprintln!("{}", ctx.trace.to_ndjson());
            }
        }

        other => {
            eprintln!("unknown subcommand: {other}");
            process::exit(2);
        }
    }
}
