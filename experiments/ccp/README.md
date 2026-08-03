# CCP — Copy-Change-Paste experiment

An end-to-end experiment that tests one claim:

> Binary data can be represented in less space by storing a common base
> structure once and recording only the differences from it, losslessly.

The experiment is built to be able to **fail**. Nothing here assumes CCP works.
Every dataset is encoded, decoded and checked against the original SHA-256, and
the reported CCP size is the measured length of a real serialised container —
not a formula.

## Layout

| File | Purpose |
| --- | --- |
| `ccp_full_experiment.py` | The engine and the single-file experiment: load, split, group, decide, encode, decode, verify, report. |
| `ccp_datasets.py` | Deterministic synthetic datasets, including the controls CCP must lose on. |
| `ccp_benchmark.py` | Runs the engine over many datasets and puts it beside gzip, bzip2 and LZMA. Measures peak RSS from the kernel. |
| `ccp_layer_analysis.py` | Per-tensor analysis of a `.safetensors` model, plus a reversible byte-plane reordering as a control transform. |
| `tests/test_ccp.py` | Anchor cases, undefined cases, hand-computed cost values, round-trip and determinism checks. |
| `FINDINGS.md` | What the experiment actually measured and what it means. |

Only the standard library is needed. `ccp_datasets.py` and the engine have no
third-party imports at all.

## Running it

One file, full chain, report to `ccp_full_report.txt`:

```bash
python3 ccp_full_experiment.py /path/to/model.gguf
```

Sweep only some region sizes, and keep machine-readable output:

```bash
python3 ccp_full_experiment.py model.safetensors --sizes 64KB,1MB --json out.json
```

The negative control — incompressible data, where CCP must not show a saving:

```bash
python3 ccp_full_experiment.py --random 64MB
```

Full dataset comparison against general-purpose compressors. Any file dropped in
`data/` is picked up automatically:

```bash
python3 ccp_benchmark.py --size 64MB --real-dir data --out-dir reports
```

Per-tensor breakdown of a real model:

```bash
python3 ccp_layer_analysis.py data/model.safetensors --min-size 1MB
```

Tests:

```bash
python3 -m unittest discover -s tests -t .
```

## What the engine does

1. **Load.** Size and SHA-256 are recorded. The file is memory-mapped, so a
   multi-gigabyte input never has to fit in RAM.
2. **Split.** The file is cut into equal regions. Six region sizes from 4KB to
   4MB are swept rather than one being picked in advance, because the best
   region size is a property of the data and not of the method.
3. **Group.** Regions that plausibly share a base are grouped. One base can
   serve many regions, which is where a large win would have to come from.
4. **Decide.** Every region is costed both ways and stored whichever way is
   smaller — as a delta against its base, or verbatim. A region is never forced
   into a delta.
5. **Encode.** A real container is written: header, region index, bases,
   deltas, verbatim regions. Its length on disk is the reported CCP size, and
   the encoder asserts that its own accounting matches what the filesystem
   reports.
6. **Decode and verify.** The container is decoded back to a file and hashed.
   A mismatch is a hard failure and makes the run exit non-zero — a result that
   cannot be reconstructed is not a result.

The cost accounting includes everything: the container header, the per-region
index, the change positions, the changed values, and the per-delta count. There
is no term left out to make the number look better. The analytic cost model is
computed in parallel with the real encoder and the report flags it if the two
disagree, which is how encoder bugs surface as arithmetic instead of silence.

## Reading the reports

Savings are `(original - ccp) / original * 100`. Negative means the container
came out larger than the input, which is the correct outcome on data with no
exploitable structure and is reported as-is.

Every report ends in a verdict of `SAVING`, `MARGINAL`, `NO SAVING` or
`INCONCLUSIVE`. `MARGINAL` means the result is under 1% and inside the noise of
the index overhead — it is not a win.

The benchmark's `WHERE CCP DOES NOT WORK` section is not a caveat, it is a
result. A method whose failure region is unknown cannot be deployed.

## Limits worth knowing

* Regions are fixed-size and aligned to their offset. A file whose repeated
  structure is shifted by a few bytes will not be seen. Content-defined
  chunking would be the way to test that, and it is not implemented here.
* Deltas are byte-granular XOR against a single base. Chained deltas
  (a delta against a delta) are rejected by the decoder.
* Grouping is greedy and single-pass: a region joins the first cluster that
  claims it, which is not necessarily the one whose base suits it best.
* `bzip2` and `LZMA` baselines run on a 128MB prefix of larger files, and the
  benchmark marks those numbers with a `p`.
