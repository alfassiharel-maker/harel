# CCP — Copy, Change, Paste

Differential Binary Versioning & Storage Engine.

CCP stores versions of large binary artifacts as a base plus an efficiently
encoded representation of what changed, **chooses between FULL and delta storage
by computing the cost of each**, reconstructs versions exactly, and verifies the
result cryptographically.

XOR is one primitive inside the engine, not the product. `A XOR B` is the same
size as `B` — the saving comes from the change being sparse and from choosing a
representation that exploits that, which is why the cost model matters more than
the XOR.

## Status

A working vertical slice across all four languages. See `CLAUDE.md` for the
component-by-component status register, the open questions, and the assumptions
that have **not** yet been validated against real workloads.

## Build and test

```bash
make            # build everything, run every suite
make doctor     # check each component is present and runnable
make demo       # run the pipeline on generated artifacts and report measurements
```

Requires a C++17 compiler, CMake, Rust, Julia and Python 3.10+. There are no
third-party libraries in any of the four layers.

## Use

```bash
cd control
python3 -m ccp store       --repo /tmp/repo --file model_v1.safetensors --name v1
python3 -m ccp store       --repo /tmp/repo --file model_v2.safetensors --name v2 --base v1
python3 -m ccp list        --repo /tmp/repo
python3 -m ccp inspect     --repo /tmp/repo --version v2
python3 -m ccp reconstruct --repo /tmp/repo --version v2 --out /tmp/restored.bin
python3 -m ccp verify      --repo /tmp/repo
python3 -m ccp benchmark   --repo /tmp/bench --files a.bin b.bin c.bin
```

`--json` on any command prints the raw engine output. `verify` reconstructs every
stored version and checks its hash; exit code 3 means an integrity failure
specifically, as opposed to a usage error.

## Layers

| Layer | Language | Owns |
|---|---|---|
| `control/` | Python | CLI, orchestration, configuration, benchmarking, reporting |
| `dataeng/` | Rust | streaming, blocks, container format, version graph, reconstruction, integrity |
| `bitexec/` | C++ | XOR, POPCOUNT, comparison, masks, SIMD dispatch |
| `strategy/` | Julia | cost model, representation selection, thresholds, policy |

The boundaries are enforced by what each layer is allowed to contain: no bit loops
in Python, no I/O in C++, no policy in Rust, no file storage in Julia.

`docs/format-v1.md` specifies the on-disk container format and is authoritative
for it.
