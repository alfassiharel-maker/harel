//! Stage timings for a generated program.
//!
//! This exists to answer "where does the time go?" before anyone optimises
//! anything. Nothing in the engine has been optimised: 0.1 is a correctness
//! milestone, and `docs/03_EXECUTION_MODEL.md` §10 says no change is made for
//! speed without a before-and-after number and a regression test. This is the
//! tool that produces the number.
//!
//! It is deliberately not a statistical benchmark harness — no warmup curves,
//! no outlier rejection. It reports wall-clock for one run of each stage at a
//! few sizes, which is enough to see a quadratic before it hurts and honest
//! about being nothing more than that.
//!
//! Run with `cargo run --release -p lml-benchmarks`.

#![forbid(unsafe_code)]
#![warn(missing_docs)]

use lml_runtime::{run, Limits};
use std::time::Instant;

/// A program with `rules` rules in one dependency chain: rule *n* reads what
/// rule *n-1* derived. The hardest shape for a round-based engine, and the one
/// that shows the round structure's cost most clearly.
fn chain(rules: usize) -> String {
    let mut source = String::from("fact n0 = 1\n");
    for index in 1..=rules {
        source.push_str(&format!(
            "rule r{index}: when n{} > 0 then n{index} = n{} + 1\n",
            index - 1,
            index - 1
        ));
    }
    source.push_str(&format!("output n{rules}\n"));
    source
}

fn main() {
    println!("{:>8}  {:>10}  {:>10}  {:>10}  {:>10}", "rules", "parse", "analyse", "lower", "run");

    for &size in &[10_usize, 100, 500, 1000] {
        let source = chain(size);

        let start = Instant::now();
        let Ok(program) = lml_parser::parse_source(&source) else {
            println!("{size:>8}  parse failed");
            continue;
        };
        let parse = start.elapsed();

        let start = Instant::now();
        let Ok(model) = lml_semantic::analyze(program) else {
            println!("{size:>8}  analysis failed");
            continue;
        };
        let analyse = start.elapsed();

        let start = Instant::now();
        let Ok(ir) = lml_compiler::lower(&model) else {
            println!("{size:>8}  lowering failed");
            continue;
        };
        let lower = start.elapsed();

        let start = Instant::now();
        let outcome = run(&ir, Limits::new());
        let execute = start.elapsed();
        if outcome.is_err() {
            println!("{size:>8}  execution failed");
            continue;
        }

        println!(
            "{size:>8}  {:>9.2?}  {:>9.2?}  {:>9.2?}  {:>9.2?}",
            parse, analyse, lower, execute
        );
    }

    println!(
        "\nThe chain shape is quadratic by construction: a rule that becomes\n\
         evaluable late is retried once per round. Semi-naive evaluation is the\n\
         known fix, and it is not implemented, because nothing has yet shown it\n\
         is needed on a real rule base."
    );
}
