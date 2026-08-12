//! The workspace has no third-party dependencies, and this test is what makes
//! that a property rather than a claim.
//!
//! It is not asceticism. It means `git clone && cargo test` works offline on any
//! machine, that the supply-chain surface is empty, and that adding a dependency
//! is a visible decision — this test fails, and the justification required by
//! `docs/ENGINEERING_IMPLEMENTATION_SPEC.md` §30 has to be written down before it
//! can pass again.

// In a test, a panic is the reporting mechanism: `unwrap`, `expect` and
// `assert!` are how a failure is announced. They stay denied in library code
// (docs/03_EXECUTION_MODEL.md §8).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::path::Path;

#[test]
fn the_lockfile_contains_only_this_workspace() {
    let lockfile = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../Cargo.lock");
    let Ok(contents) = std::fs::read_to_string(&lockfile) else {
        // A fresh checkout that has never been built has no lockfile; there is
        // nothing to check and nothing to fail.
        return;
    };

    let foreign: Vec<&str> = contents
        .lines()
        .filter_map(|line| line.strip_prefix("name = \""))
        .filter_map(|line| line.strip_suffix('"'))
        .filter(|name| !name.starts_with("lml-"))
        .collect();

    assert!(
        foreign.is_empty(),
        "the workspace gained third-party dependencies: {foreign:?}\n\
         Adding one is allowed, but it needs the written justification of \
         docs/ENGINEERING_IMPLEMENTATION_SPEC.md §30 in docs/DEPENDENCIES.md first."
    );
}
