# CCP — Copy, Change, Paste

The CCP Core: shared structure is found between units, one **base** is kept, and
everything else is stored as a **change program** against it. Executing that
program reconstructs the unit. Reading it — without executing it — is what lets
work happen over the representation instead of over the bytes.

`docs/00-core-spec.md` is the specification of what is implemented here, with
every element classified as defined-by-user, engineering decision, or open
question. Read it before changing the Core.

## Status

```
ccp.core          the algorithm and its representation      IMPLEMENTED
ccp.capabilities  work performed on the representation      IMPLEMENTED
ccp.runtime       execution strategy under a contract       NOT BUILT
ccp.integration   project import, language analysis         NOT BUILT
ccp.product       build orchestration, packaging            NOT BUILT
ccp.ui            the commercial surface                    NOT BUILT
```

Layers that are not built are absent from the tree rather than stubbed, so the
package cannot be mistaken for a working system with unfinished parts. `ccp.core`
imports only the standard library.

## Using it

```bash
python3 -m ccp.cli build <directory> --out model.ccp   # analyse, represent, verify
python3 -m ccp.cli stat model.ccp                      # per-unit breakdown
python3 -m ccp.cli verify model.ccp                    # reconstruct everything
python3 -m ccp.cli extract model.ccp <uid> --out file  # one unit, in full
python3 -m ccp.cli read model.ccp <uid> --offset 8000 --length 256
```

`read` is the one worth looking at. It prints the work the read actually cost:

```
bytes touched 256 of 16017 (1.60%); instructions 1/3
```

The window is materialised by executing only the instructions that overlap it.
A compressed stream cannot do this — it must be inflated from the beginning to
reach byte 8,000.

As a library:

```python
from ccp.core import InMemoryUnitSource, build_model, serialize
from ccp.capabilities import CCPReader

source = InMemoryUnitSource({"a": data_a, "b": data_b})
model = build_model(source)
assert model.materialize("b") == data_b          # verified against its digest
window = CCPReader(model).read_range("b", 8000, 256)
print(window.bytes_touched, window.unit_size)
open("model.ccp", "wb").write(serialize(model))
```

Any other unit level — functions, tensors, records — is a new `UnitSource` and
changes nothing else in the Core.

## Tests

```bash
python3 -m unittest discover -s ccp/tests -t .
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
