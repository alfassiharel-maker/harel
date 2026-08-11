# CCP — Copy, Change, Paste

The CCP Core: shared structure is found between units, one **base** is kept, and
everything else is stored as a **change program** against it. Executing that
program reconstructs the unit. Reading it — without executing it — is what lets
work happen over the representation instead of over the bytes.

`docs/00-core-spec.md` specifies the Core; `docs/01-runtime.md` the Runtime and
the semantic contract; `docs/02-product-engine.md` the Product Engine. Every
element is classified IMPLEMENTED / NOT SUPPORTED / OPEN. Read them before
changing a layer.

## Status

```
ccp.core          the algorithm and its representation      IMPLEMENTED
ccp.capabilities  work performed on the representation      IMPLEMENTED
ccp.runtime       loading, integrity, selective access,     IMPLEMENTED
                  work accounting, under a stated contract
ccp.api           the stable public interface               IMPLEMENTED
ccp.product       artifact lifecycle, typed errors,         IMPLEMENTED
                  per-operation observability
ccp.integration   project import, language analysis         NOT BUILT
ccp.ui            the commercial surface                    NOT BUILT
```

Layers that are not built are absent from the tree rather than stubbed, so the
package cannot be mistaken for a working system with unfinished parts. `ccp.core`
imports only the standard library. External callers use `ccp.product` for
artifact-oriented access or `ccp.api` for the runtime surface; neither reaches
into `ccp.core`, and the CLI goes through `ccp.api` too, so it is exercised by a
real consumer.

## Using it

```bash
python3 -m ccp.cli build <directory> --out model.ccp   # analyse, represent, verify
python3 -m ccp.cli info model.ccp                      # sizes
python3 -m ccp.cli stat model.ccp                      # per-unit breakdown
python3 -m ccp.cli verify model.ccp                    # reconstruct everything
python3 -m ccp.cli extract model.ccp <uid> --out file  # one unit, in full
python3 -m ccp.cli read model.ccp <uid> --offset 8000 --length 256
python3 -m ccp.cli contract                            # the semantic contract
```

`read` is the one worth looking at. It prints the work the read actually cost:

```
bytes touched 256 of 16017 (1.60%); instructions 1/3
```

The window is materialised by executing only the instructions that overlap it.
A compressed stream cannot do this — it must be inflated from the beginning to
reach byte 8,000.

As a library — the product layer, with a lifecycle and typed errors:

```python
from ccp.product import ProductEngine

engine = ProductEngine()
with engine.open("model.ccp", verify=True) as artifact:   # raises on a bad artifact
    whole  = artifact.materialize("src/main.py")           # bit-exact, digest-checked
    window = artifact.read_range("src/main.py", 8000, 256) # only the covering work
    print(window.data, window.report.bytes_touched, window.report.work_ratio)
    print(artifact.ledger.summary())
```

Or the lower-level runtime surface directly via `ccp.api` (`build.from_directory`,
`open_representation`). Any other unit level — functions, tensors, records — is a
new `UnitSource` and changes nothing else in the Core.

## Tests and benchmarks

```bash
python3 -m unittest discover -s ccp/tests -t .
python3 -m ccp.benchmarks.runtime_benchmark <directory>
python3 -m ccp.benchmarks.product_benchmark <directory>
```

No dependencies. Fixtures are hash-derived, never `random`: a flaky result would
make a failure impossible to interpret.

## What this is not

Measured on five revisions of this repository's `backend/` tree (111 units,
723 KB), the container is **37.78% smaller** than the input with all 111 units
verified — and **gzip and xz both beat it on size**. That is expected. CCP removes
duplication *between* units; an entropy coder removes redundancy *inside* one.
They compose: CCP then gzip beats gzip alone by 36% on the same input. The full
table is in `docs/00-core-spec.md` §10.

So: not a compressor, not a Python optimiser, not a `ZIP → EXE` wrapper. What it
provides is a representation that keeps shared structure explicit and addressable,
and lets work be done against that structure without materialising it.

## Relationship to `experiments/ccp/`

That directory is research and stays research. It measured whether base+delta
saves storage (`FINDINGS.md`) and whether it can cut execution work in a narrow
linear case (`FINDINGS_EXECUTION.md`). Its results are **evidence**, and two of
them are designed into this Core directly: content-defined chunking exists here
because fixed regions were measured to be blind to shifted duplicates, and the
change program replaces XOR pairs because those were measured to collapse under
dense change. The experiments are not part of the product code path.
