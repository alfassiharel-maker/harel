#!/usr/bin/env bash
# Enforce the dependency rules of docs/05_ARCHITECTURE.md §3.
#
# Cargo already rejects cycles. What it does not check is *intent*: an edge that
# is acyclic but forbidden — the runtime learning about SQLite, the logic engine
# learning about a window — compiles perfectly and destroys the architecture.
# This script fails the build on those.
#
# Every rule below is one grep, and every rule states what it protects.

set -euo pipefail
cd "$(dirname "$0")/.."

failures=0

fail() {
    echo "ARCHITECTURE: $1" >&2
    failures=$((failures + 1))
}

# The declared dependencies of a crate, from its manifest.
deps_of() {
    sed -n '/^\[dependencies\]/,/^\[/p' "crates/$1/Cargo.toml" | grep -oE '^lml-[a-z]+' || true
}

# --- 1. Nothing depends on the CLI. It is a consumer, never a component. -----
for manifest in crates/*/Cargo.toml tests/e2e/Cargo.toml benchmarks/Cargo.toml; do
    case "$manifest" in crates/cli/*) continue ;; esac
    if grep -q '^lml-cli' "$manifest"; then
        fail "$manifest depends on lml-cli"
    fi
done

# --- 2. No core crate knows about a UI. --------------------------------------
if grep -rniE '\b(tauri|electron|react|typescript|webview)\b' crates/ --include='*.rs' \
        --include='*.toml' >/dev/null 2>&1; then
    fail "a core crate mentions a UI technology (docs/05 §3, invariant 2)"
fi

# --- 3. No core crate knows about a database. --------------------------------
# When crates/sql exists it will sit behind a trait defined in lml-logic; until
# then, no core crate has any business naming a database engine.
if grep -rniE '\b(sqlite|postgres|duckdb|libpq|rusqlite)\b' crates/ --include='*.rs' \
        --include='*.toml' | grep -v '^crates/[a-z]*/src/lib.rs:.*docs/' >/dev/null 2>&1; then
    fail "a core crate mentions a database engine (docs/05 §3, invariant 3)"
fi

# --- 4. Representation does not depend on strategy. --------------------------
if deps_of logic | grep -q '^lml-reasoning$'; then
    fail "lml-logic depends on lml-reasoning; representation must not know about strategy"
fi

# --- 5. The early stages do not depend on the late ones. ---------------------
for crate in diagnostics lexer ast parser types semantic ir; do
    for forbidden in lml-runtime lml-reasoning lml-compiler lml-cli; do
        if deps_of "$crate" | grep -q "^${forbidden}$"; then
            fail "lml-$crate depends on $forbidden; the pipeline flows one way"
        fi
    done
done

# --- 6. diagnostics is a leaf. -----------------------------------------------
if [ -n "$(deps_of diagnostics)" ]; then
    fail "lml-diagnostics has dependencies; it must stay a leaf"
fi

# --- 7. No unsafe anywhere. --------------------------------------------------
if grep -rn 'unsafe' crates/ benchmarks/ tests/e2e/ --include='*.rs' \
        | grep -v 'forbid(unsafe_code)' | grep -v 'unsafe_code = "forbid"' >/dev/null 2>&1; then
    fail "unsafe code appeared; adding any needs the justification of the engineering spec §29"
fi

# --- 8. No third-party dependencies. -----------------------------------------
if grep -rhE '^[a-z0-9_-]+ *= *"' crates/*/Cargo.toml tests/e2e/Cargo.toml benchmarks/Cargo.toml \
        | grep -vE '^(version|edition|name|description|license|rust-version|path|repository|publish)' \
        >/dev/null 2>&1; then
    fail "a versioned (non-path) dependency appeared; see docs/ENGINEERING_IMPLEMENTATION_SPEC.md §30"
fi

# --- 9. No file called utils.rs or helpers.rs. -------------------------------
# A file with no responsibility is where responsibility goes to hide.
if find crates tests benchmarks -name 'utils.rs' -o -name 'helpers.rs' -o -name 'misc.rs' \
        | grep -q .; then
    fail "a utils/helpers/misc module appeared (docs/05 §4)"
fi

if [ "$failures" -gt 0 ]; then
    echo "architecture check failed with $failures problem(s)" >&2
    exit 1
fi

echo "architecture check passed"
