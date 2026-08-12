.DEFAULT_GOAL := help
SHELL := /bin/bash

.PHONY: help
help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

.PHONY: check
check: ## Type-check the whole workspace
	cargo check --workspace --all-targets

.PHONY: test
test: ## Run every test, including the language fixtures
	cargo test --workspace

.PHONY: fmt
fmt: ## Format
	cargo fmt --all

.PHONY: fmt-check
fmt-check: ## Fail if anything is unformatted
	cargo fmt --all --check

.PHONY: lint
lint: ## Clippy, warnings are errors
	cargo clippy --workspace --all-targets -- -D warnings

.PHONY: lint-arch
lint-arch: ## Enforce the dependency rules of docs/05_ARCHITECTURE.md
	./scripts/check-architecture.sh

.PHONY: verify
verify: fmt-check lint lint-arch test ## Everything CI runs
	@echo "verified"

.PHONY: run-example
run-example: ## Run the canonical program and show its trace
	cargo run -q -p lml-cli -- run examples/temperature.lml
	cargo run -q -p lml-cli -- trace examples/temperature.lml
	cargo run -q -p lml-cli -- why examples/temperature.lml status

.PHONY: bench
bench: ## Stage timings (release build)
	cargo run --release -q -p lml-benchmarks

.PHONY: clean
clean: ## Remove build output
	cargo clean
