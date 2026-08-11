# CCP Product Engine — public contract

Describes working code in `ccp/product/`. The Product Engine is the application
layer: it turns the Runtime into an *artifact* a program opens, uses under an
explicit lifecycle, and closes. It adds a lifecycle, a typed error model, and
per-operation observability — and it adds **no algorithm and no second
reconstruction path**. Every byte comes from the Runtime, which gets it from the
Core, reached through the public API (`ccp.api`) exactly as any external
integration would.

Sections are marked **IMPLEMENTED**, **NOT SUPPORTED**, or **OPEN**.

---

## Boundary

```
ccp.core          representation, base selection, change programs      (algorithm)
ccp.capabilities  selective reads over the representation              (algorithm)
ccp.runtime       load, verify, materialize, read_range, accounting    (operations)
ccp.api           the stable public surface                            (interface)
ccp.product       artifacts: lifecycle, typed errors, observability    (application)  <- this
```

The Product Engine imports `ccp.api` and `ccp.product` internals only. It does not
import `ccp.core`. The smallest product boundary consistent with the two
source-of-truth PDFs is *an artifact you can open, validate, read selectively, and
close, with typed errors and measured work* — not a build system, packager, or UI,
which the product direction places in later layers.

## Object lifecycle — IMPLEMENTED

`ProductEngine` is a factory; it holds no artifact state and tracks nothing it
hands out, so there is no global or hidden state.

| Call | Effect |
| --- | --- |
| `engine.open(path_or_bytes, verify=False)` | open an existing artifact |
| `engine.create_from_directory(path, save_to=None, verify=True)` | build from files, open the result |
| `engine.create_from_units(mapping, save_to=None, verify=True)` | build from memory, open the result |
| `engine.build_from_directory / build_from_units` | build artifact bytes only, open nothing |

`CCPArtifact` owns exactly one Runtime and one instance-scoped ledger:

| Call | Effect |
| --- | --- |
| `artifact.info()` / `units()` / `groups()` / `stat(uid)` | inspect without materialising |
| `artifact.validate()` | reconstruct and digest-check every unit; raises on failure |
| `artifact.close()` | release the Runtime; idempotent; operations after it raise |
| `with engine.open(...) as artifact:` | close on exit |

Ownership is deterministic and explicit: `close()` drops the Runtime reference at
a point the caller controls, rather than leaving it to a finaliser.

## Data access — IMPLEMENTED

| Call | Returns | Verification |
| --- | --- | --- |
| `materialize(uid)` | `MaterializeResult(data, report)` | whole unit, digest-checked |
| `read_range(uid, offset, length)` | `ReadResult(data, report)` | slice, not re-checked |
| `read_many(requests)` | `BatchReadResult(reads, report)` | slices; results in request order |
| `units_equal(a, b)` | `Optional[bool]` | metadata only |

## Selective access — IMPLEMENTED

`read_range` is served by executing only the change-program instructions whose
output overlaps the window. **The whole artifact is never materialised to serve a
part.** This is delegated to the Runtime, which delegates to the Core; the product
adds the measurement and the stable surface, not the mechanism.

Measured (`ccp/benchmarks/reports/product_backend_revisions.txt`), 111 units,
723.14 KB:

| window | requested | touched | of unit | instructions | verify |
| --- | --- | --- | --- | --- | --- |
| 64 B | 64 | 64 | 0.23% | 1/1 | slice_unverified |
| 256 B | 256 | 256 | 0.94% | 1/1 | slice_unverified |
| 4 KB | 4096 | 4096 | 14.98% | 1/1 | slice_unverified |
| full | 27,347 | 27,347 | 100.00% | 1/1 | digest_checked |

Corpus sweep, one 256-byte product read from every unit: **26,203 bytes touched
against 740,496 total (3.539%), 0 mismatches.**

## Resource management — IMPLEMENTED

* **Bounded memory**: the engine refuses an artifact larger than
  `max_artifact_bytes` (default 2 GiB) with a `ResourceError`, rather than letting
  the allocator kill the process. Version 1 loads the whole representation.
* **Deterministic ownership**: one artifact owns one Runtime; `close()` releases
  it explicitly.
* **No hidden global state**: engines and artifacts are plain instances; two
  artifacts share nothing, so one's ledger and lifecycle never affect another's.

## Error model — IMPLEMENTED

Every failure is a typed subclass of `ProductError`, each keeping the originating
exception on `.cause`:

| Condition | Error |
| --- | --- |
| malformed / truncated artifact | `MalformedArtifactError` |
| unknown unit id | `UnitNotFoundError` (carries `.uid`) |
| negative offset or length | `InvalidRangeError` |
| reconstruction ≠ digest | `VerificationError` |
| operation the contract does not offer | `UnsupportedOperationError` (carries `.operation`) |
| closed artifact, oversize, unreadable file | `ResourceError` |

A caller catches `ProductError` for everything, or a specific subclass for one
case. No bare `ValueError` or `KeyError` escapes the product surface.

## Observability — IMPLEMENTED

Every operation returns an `OperationReport`:

| Field | Meaning |
| --- | --- |
| `bytes_requested` | what the caller asked for |
| `bytes_returned` | what came back |
| `bytes_touched` | what the operation actually read |
| `units_touched` | distinct units involved |
| `instructions_visited` / `instructions_total` | reconstruction work |
| `verification` | `digest_checked` / `slice_unverified` / `not_applicable` |
| `artifact_verified` | whether the whole artifact has been validated this session |
| `elapsed_seconds` | wall-clock, reported only |

`work_ratio = bytes_touched / bytes_returned` exposes the selective-access
advantage as a measured number. **Correctness never depends on `elapsed_seconds`,
and no test asserts on it.** Reports aggregate into an instance-scoped
`ProductLedger` (`artifact.ledger`, `reset_ledger()`).

## Determinism — IMPLEMENTED

Identical artifact + identical request → identical output, regardless of request
order or of what else the artifact has served. The product adds no state that
affects output; it delegates to the Runtime, whose reads are order-independent.
Tested directly, including a "busy" artifact that serves every other unit first
and still returns byte-identical results.

## NOT SUPPORTED (version 1)

* Mutating a loaded artifact — add / remove / update a unit.
* Returning content that is not the original bytes (any transform).
* Operating on a partially-available artifact (streaming an incomplete container).
* Executing a target program, or deciding how one should run.

Each raises, or is simply absent; none is faked.

## OPEN

* **Concurrency.** The semantic contract lists concurrent access to one instance
  as unsupported, so the product does not provide it and does not test it as
  supported — a test asserts the contract still marks it unsupported, so the gap
  cannot be silently closed. Artifacts share no state, so *separate* artifacts on
  separate threads are fine; one artifact across threads is OPEN.
* **A looser semantic contract**, where output may legitimately differ from the
  original bytes (allowed/required behaviour, permitted approximation). Version 1
  is strict and bit-exact. Widening it is a design decision, not an implementation
  gap, and is not invented here.
* **Lazy / memory-mapped loading**, so an artifact larger than memory can be
  opened. Version 1 loads the whole container and refuses one over the limit.

## Using it

```python
from ccp.product import ProductEngine

engine = ProductEngine()
with engine.open("model.ccp", verify=True) as artifact:
    info = artifact.info()
    window = artifact.read_range("src/main.py", 8000, 256)
    print(window.data, window.report.bytes_touched, window.report.work_ratio)
    print(artifact.ledger.summary())
```
