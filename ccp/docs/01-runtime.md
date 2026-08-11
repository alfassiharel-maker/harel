# CCP Runtime — specification of what is implemented

Describes working code in `ccp/runtime/` and `ccp/api/`. The Runtime turns the
Core from an algorithm into usable infrastructure: it loads a representation,
establishes its integrity, serves work against it, and reports what that work
cost.

The Runtime contains **no algorithm**. Every byte it returns is produced by
`ccp.core` / `ccp.capabilities`. There is no second implementation that
reproduces what the Core does, because two implementations of one algorithm can
disagree and nothing would say which is right.

---

## 1. The distinction the Runtime exists to make real

```
representation -> selective reconstruction          <- what this does
representation -> reconstruct everything -> slice   <- what it does not do
```

A `read_range` executes only the instructions whose output overlaps the window.
Measured on the corpus below: a 256-byte read from every one of 111 units touched
**26,203 bytes against 740,496** for the same reads served by full
reconstruction — **3.54%** of the work, with zero mismatches.

This is the property that distinguishes CCP from a compression runtime. A
compressed stream must be inflated from its start to reach byte 8,000. A CCP unit
reaches it by executing one instruction.

## 2. Semantic contract v1.0

Minimal by intent, and machine-readable at `ccp.api.CONTRACT`
(`python3 -m ccp.cli contract`). The whole of it in one sentence: **the Runtime
may change how much work it does, and nothing about what it returns.**

| | |
| --- | --- |
| **Input** | A CCP container from `ccp.core`, as bytes or a path. Self-describing and self-verifying: it carries its chunking configuration and a digest per unit. |
| **May change** | How many instructions it executes; how many bytes it reads; the order it visits instructions within one request; whether it materialises a whole unit or part of one. |
| **Must be bit-exact** | `materialize(uid)` equals the original unit byte for byte. `read_range(uid, o, n)` equals `materialize(uid)[o:o+n]`. Results never depend on request order, on how many requests preceded them, or on timing. |
| **Valid output** | Bytes identical to the original or to the requested slice. A full materialisation is additionally checked against its recorded digest and fails loudly rather than returning bytes that do not match. |

`CCP_Technical_Clarifications` §7 says the criterion is not automatically
`output == original output`. Version 1 takes the **strictest** position available
— every returned byte bit-exact — because that is the position the Core can
already verify. A looser contract, where output may legitimately differ, is a
real design question and is listed as open rather than invented.

**Supported**: `load`, `verify`, `materialize`, `read_range`, `read_many`,
`units`, `stat`, `groups`, `ledger`.

**Not supported yet**: mutating a loaded representation; returning content that
is not the original bytes; operating on a partially-available container;
executing a target program or deciding how one should run; concurrent access to
one runtime instance.

**Open design questions, excluded from the MVP**: a contract permitting output to
differ from the original; execution management; lazy/memory-mapped loading for
representations larger than memory (v1 loads the whole container and refuses one
above a stated limit rather than being killed by the allocator).

## 3. Range reads and verification — the honest boundary

A digest covers a **whole unit**. A range read therefore cannot re-verify the
unit on its own; it returns a slice of the representation as loaded. `verify()`
reconstructs and checks every unit and flips `is_verified`. The contract says so
explicitly rather than implying that every read is independently authenticated.

For anything from outside the caller's control, `open_representation(..., verify=True)`
does the full pass before returning, and raises rather than handing back a
runtime over a damaged representation.

## 4. Work accounting

Every operation records `bytes_touched`, `bytes_returned`,
`instructions_visited`, `instructions_total`, `unit_size`, `elapsed_seconds`,
aggregated in a `WorkLedger`. Counts come from the Core's own report of what it
did; the Runtime aggregates and does not re-derive them.

`elapsed_seconds` is reported because a caller needs it. **It is never used to
decide anything, and no assertion in the test suite depends on it.**

`materialize` records `bytes_touched == unit_size` — the honest baseline every
selective read is measured against.

## 5. Layering

```
ccp.core          representation, base selection, change programs,
                  serialization, verification                        IMPLEMENTED
ccp.capabilities  work performed on the representation               IMPLEMENTED
ccp.runtime       loading, reconstruction, range access, execution
                  of representation instructions, work accounting    IMPLEMENTED
ccp.api           stable public interface, no internals exposed      IMPLEMENTED
ccp.integration   project import, language analysis                  NOT BUILT
ccp.product       build orchestration, packaging                     NOT BUILT
ccp.ui            the commercial surface                             NOT BUILT
```

`ccp/cli.py` goes through `ccp.api`, not through the Core — if the API is not
sufficient for the CLI, it is not sufficient for anyone. Layers not built are
absent from the tree rather than stubbed.

## 6. Measured — `ccp/benchmarks/reports/runtime_backend_revisions.txt`

Real input: five successive revisions of this repository's `backend/` tree,
111 files, 723.14 KB.

| | measured |
| --- | --- |
| units | 111 (60 full, 51 derived) |
| original | 723.14 KB |
| representation | 449.95 KB (payload 440.86 + index 9.09) |
| saving | **37.78%**, index included |
| build | 233.2 ms |
| load | 0.62 ms |
| verify | 111/111 units, 723.14 KB, 1.4 ms |

Selective read from a 26.71 KB derived unit:

| window | bytes touched | of unit | instructions | latency | correct |
| --- | --- | --- | --- | --- | --- |
| 64 B | 64 | 0.23% | 1/1 | 0.0096 ms | yes |
| 256 B | 256 | 0.94% | 1/1 | 0.0068 ms | yes |
| 4 KB | 4096 | 14.98% | 1/1 | 0.0070 ms | yes |
| full | 27,347 | 100.00% | 1/1 | 0.0462 ms | yes |

Corpus sweep, 256-byte read from every unit: **26,203 bytes touched vs 740,496
for full reconstruction (3.54%), 113 instructions, 0 mismatches.**

Correctness in that table is byte comparison against a full reconstruction, not
an expectation computed by the benchmark, and it does not depend on any timing.

## 7. Using it

```python
from ccp.api import build, open_representation

container = build.from_directory("./project")
rt = open_representation(container, verify=True)

whole  = rt.materialize("src/main.py")            # bit-exact, digest-checked
window = rt.read_range("src/main.py", 8000, 256)  # only the covering work
print(window.bytes_touched, window.unit_size, window.instructions_visited)
print(rt.ledger.summary())
```
