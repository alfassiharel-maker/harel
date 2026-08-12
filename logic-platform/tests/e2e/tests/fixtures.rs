//! The fixture harness.
//!
//! For every `tests/runtime/<name>.lml` there is a `<name>.expected` holding the
//! exact plain-text result. A fixture may also have a `<name>.expected-trace`,
//! which is compared as a snapshot.
//!
//! Adding a language test is adding two text files, which is the point: there is
//! no excuse not to add one (`docs/05_ARCHITECTURE.md` §5).
//!
//! Failure fixtures end in `.lml` with an `.expected` whose first line is
//! `error: <CODE>`; they assert that the program is rejected or fails with that
//! code, and nothing else.

// In a test, a panic is the reporting mechanism: `unwrap`, `expect` and
// `assert!` are how a failure is announced. They stay denied in library code
// (docs/03_EXECUTION_MODEL.md §8).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use lml_compiler::compile;
use lml_runtime::{run, Limits};
use std::path::{Path, PathBuf};

fn fixture_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../runtime")
}

/// The result of running one fixture, in the same plain text `lml run` prints.
fn execute(source: &str) -> Result<String, String> {
    let compilation = compile(source).map_err(|diagnostics| {
        diagnostics.as_slice().first().map_or_else(
            || "error: unknown".to_owned(),
            |d| format!("error: {}", d.code),
        )
    })?;
    match run(&compilation.ir, Limits::new()) {
        Ok(execution) => Ok(execution.to_text()),
        Err(failure) => Err(format!("error: {}", failure.diagnostic.code)),
    }
}

fn fixtures() -> Vec<PathBuf> {
    let mut paths: Vec<PathBuf> = std::fs::read_dir(fixture_dir())
        .expect("tests/runtime exists")
        .filter_map(|entry| entry.ok().map(|entry| entry.path()))
        .filter(|path| path.extension().is_some_and(|extension| extension == "lml"))
        .collect();
    // Sorted so that a failure names the same fixture on every machine.
    paths.sort();
    assert!(
        !paths.is_empty(),
        "no fixtures found in {}",
        fixture_dir().display()
    );
    paths
}

#[test]
fn every_fixture_produces_its_expected_output() {
    let mut failures = Vec::new();

    for path in fixtures() {
        let source = std::fs::read_to_string(&path).expect("fixture is readable");
        let expected_path = path.with_extension("expected");
        let expected = std::fs::read_to_string(&expected_path)
            .unwrap_or_else(|_| panic!("{} has no .expected file", path.display()));

        let actual = match execute(&source) {
            Ok(text) | Err(text) => text,
        };
        if actual.trim_end() != expected.trim_end() {
            failures.push(format!(
                "{}\n  expected: {:?}\n  actual:   {:?}",
                path.display(),
                expected.trim_end(),
                actual.trim_end()
            ));
        }
    }

    assert!(
        failures.is_empty(),
        "fixtures failed:\n{}",
        failures.join("\n")
    );
}

#[test]
fn trace_snapshots_match() {
    let mut failures = Vec::new();

    for path in fixtures() {
        let snapshot_path = path.with_extension("expected-trace");
        let Ok(expected) = std::fs::read_to_string(&snapshot_path) else {
            continue;
        };

        let source = std::fs::read_to_string(&path).expect("fixture is readable");
        let compilation = compile(&source).unwrap_or_else(|d| panic!("{}: {d}", path.display()));
        let trace = match run(&compilation.ir, Limits::new()) {
            Ok(execution) => execution.trace,
            Err(failure) => failure.trace,
        };

        if trace.to_text().trim_end() != expected.trim_end() {
            failures.push(format!("{}\n{}", path.display(), trace.to_text()));
        }
    }

    assert!(
        failures.is_empty(),
        "trace snapshots failed:\n{}",
        failures.join("\n")
    );
}

#[test]
fn ir_snapshots_match() {
    // `docs/05_ARCHITECTURE.md` §5 advertised `.expected-ir` fixtures before
    // the harness could read them — audit contradiction C6. It can now.
    let mut failures = Vec::new();

    for path in fixtures() {
        let snapshot_path = path.with_extension("expected-ir");
        let Ok(expected) = std::fs::read_to_string(&snapshot_path) else {
            continue;
        };
        let source = std::fs::read_to_string(&path).expect("fixture is readable");
        let compilation = compile(&source).unwrap_or_else(|d| panic!("{}: {d}", path.display()));
        let actual = lml_ir::print_ir(&compilation.ir);
        if actual.trim_end() != expected.trim_end() {
            failures.push(format!("{}\n{actual}", path.display()));
        }
    }

    assert!(
        failures.is_empty(),
        "IR snapshots failed:\n{}",
        failures.join("\n")
    );
}

#[test]
fn every_execution_is_reproducible() {
    // Determinism is a language guarantee (docs/01 §6), so it is checked over
    // every fixture rather than in one example.
    for path in fixtures() {
        let source = std::fs::read_to_string(&path).expect("fixture is readable");
        let Ok(compilation) = compile(&source) else {
            continue;
        };
        let first = run(&compilation.ir, Limits::new());
        let second = run(&compilation.ir, Limits::new());
        match (first, second) {
            (Ok(first), Ok(second)) => {
                assert_eq!(
                    first.trace,
                    second.trace,
                    "{}: traces differ",
                    path.display()
                );
                assert_eq!(
                    first.outputs,
                    second.outputs,
                    "{}: outputs differ",
                    path.display()
                );
            }
            (Err(first), Err(second)) => {
                assert_eq!(first.diagnostic, second.diagnostic, "{}", path.display());
                assert_eq!(first.trace, second.trace, "{}", path.display());
            }
            _ => panic!(
                "{}: one run succeeded and the other did not",
                path.display()
            ),
        }
    }
}

#[test]
fn compilation_is_reproducible_down_to_the_digest() {
    for path in fixtures() {
        let source = std::fs::read_to_string(&path).expect("fixture is readable");
        let (Ok(first), Ok(second)) = (compile(&source), compile(&source)) else {
            continue;
        };
        assert_eq!(
            lml_runtime::digest(&first.ir),
            lml_runtime::digest(&second.ir),
            "{}: the program digest is not stable",
            path.display()
        );
    }
}
