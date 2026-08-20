# CCP build driver.
#
# Four toolchains, one entry point. `make` alone builds everything and runs every
# suite, because a four-language project where the languages are built separately
# is where cross-boundary breakage hides.

.DEFAULT_GOAL := all
SHELL := /bin/bash

CMAKE  ?= cmake
CARGO  ?= cargo
JULIA  ?= julia
PYTHON ?= python3

BITEXEC_BUILD := bitexec/build
ENGINE        := dataeng/target/release/ccp-engine

.PHONY: all
all: build test ## Build every component and run every suite

.PHONY: help
help: ## List targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

.PHONY: build
build: build-bitexec build-dataeng ## Build the C++ engine and the Rust engine
	@echo "built. Julia and Python need no build step."

.PHONY: build-bitexec
build-bitexec: ## Build the C++ bit execution engine and its test binary
	$(CMAKE) -S bitexec -B $(BITEXEC_BUILD) -DCMAKE_BUILD_TYPE=Release
	$(CMAKE) --build $(BITEXEC_BUILD) -j

# The Rust build compiles and links the C++ engine itself (dataeng/build.rs), so
# this target does not depend on build-bitexec. That target exists to produce the
# native test binary, which the Rust build has no reason to build.
.PHONY: build-dataeng
build-dataeng: ## Build the Rust data/storage engine (compiles and links the C++ engine)
	cd dataeng && $(CARGO) build --release

# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

.PHONY: test
test: test-bitexec test-dataeng test-strategy test-integration ## Run every suite

.PHONY: test-bitexec
test-bitexec: build-bitexec ## C++ engine: XOR/popcount correctness at every SIMD boundary
	./$(BITEXEC_BUILD)/test_bitexec

.PHONY: test-dataeng
test-dataeng: ## Rust engine: format, SHA-256 vectors, JSON, FFI, strategy protocol
	cd dataeng && $(CARGO) test --release

.PHONY: test-strategy
test-strategy: ## Julia engine: the cost model and its decision boundaries
	$(JULIA) --startup-file=no strategy/test/runtests.jl

.PHONY: test-integration
test-integration: build ## The full stack on real files, byte-for-byte
	PYTHONPATH=control $(PYTHON) -m pytest -c pytest.ini tests -q

# ---------------------------------------------------------------------------
# Use
# ---------------------------------------------------------------------------

.PHONY: doctor
doctor: ## Check every component is present and runnable
	PYTHONPATH=control $(PYTHON) -m ccp doctor

.PHONY: demo
demo: build ## Store a version series on generated artifacts and report the measurements
	$(PYTHON) scripts/demo.py

.PHONY: clean
clean: ## Remove build outputs
	rm -rf $(BITEXEC_BUILD) dataeng/target
	find . -name __pycache__ -type d -exec rm -rf {} +
