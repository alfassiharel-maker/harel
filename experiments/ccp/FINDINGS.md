# CCP — what the experiment measured

Every number here comes from a run in `reports/`, on a real file, verified
byte-for-byte by SHA-256. Nothing is projected or extrapolated. Where a method
loses, the losing number is printed.

## The question

> Is there structural redundancy in AI model weights that a Copy-Change-Paste
> representation can exploit, so the weights can be stored in less space with no
> loss?

## The answer, in one paragraph

For a **single** set of model weights: no. Across **two near-identical
checkpoints**: yes, up to 49.99% against a 50% ceiling — but a general-purpose
compressor given a large enough window reaches the same place and slightly
further. The one advantage that survived scrutiny is not compression ratio, it is
speed: on near-duplicate checkpoints CCP followed by gzip lands within 0.34
percentage points of large-dictionary LZMA while encoding about 12× faster and
decoding about 4× faster. The condition attached to that is strict — the update
between the two checkpoints has to move few *bytes*, and ordinary fine-tuning
moves nearly all of them.

---

## 1. A single checkpoint yields nothing

From `reports/ccp_benchmark_report.txt`, real files, all verified:

| File | Size | CCP | gzip‑6 | LZMA‑6 |
| --- | --- | --- | --- | --- |
| bert-tiny.bin | 16.93 MB | −0.00% | 7.42% | 8.09% |
| distilbert-fp32.safetensors | 255.54 MB | −0.00% | 7.78% | 9.42%ᵖ |
| qwen2.5-0.5b-q8_0.gguf | 644.41 MB | 0.06% | 4.89% | 6.93%ᵖ |
| gpt2-fp32.safetensors | 522.71 MB | 5.99% | 15.55% | 22.23%ᵖ |

ᵖ measured on the first 128MB.

CCP is beaten by plain gzip on every one of them.

### The GPT-2 result is not what it looks like

GPT-2 is the only real file where CCP found anything, and the reason disqualifies
it as evidence about weights. Its structural row reports 8188 regions stored as
deltas carrying **zero** changed bytes — exact duplicates, not near-misses.
Attributing those regions back to tensors:

```
1023 duplicate 4KB regions in h.0.attn.bias
1023 duplicate 4KB regions in h.1.attn.bias
1023 duplicate 4KB regions in h.2.attn.bias
...  the same for every one of the 12 layers
```

`attn.bias` is the causal attention mask: a constant lower-triangular buffer,
identical in every layer, that is not a learned parameter at all. The arithmetic
closes exactly:

* duplicated mask regions: 8188 × 4KB = **32.0 MB**
* CCP saving on the file: 5.99% of 522.7 MB = **31.3 MB**
* learned parameters in the file: **474.6 MB**, saving on them: **0%**

So the entire GPT-2 win is deduplication of a precomputed constant that current
versions of the library do not even serialise — they rebuild it at load time. The
honest reading is that CCP found a wasteful checkpoint, not redundant weights.

### Per-tensor confirmation

`reports/ccp_layer_gpt2.txt` tests the ten largest tensors individually:

| | CCP | CCP after byte-plane reordering | gzip | gzip after reordering |
| --- | --- | --- | --- | --- |
| 228.24 MB of fp32 tensors | −0.00% | −0.00% | 7.09% | **14.77%** |

Every tensor individually returns −0.00%, including the 147MB embedding matrix.
The reordering column is the diagnostic: grouping the exponent bytes of adjacent
floats together *doubles* what gzip recovers, and leaves CCP at exactly zero.

That localises the redundancy precisely. It is in the **marginal distribution of
byte values** — fp32 weights clustered near zero share an exponent byte, so that
byte plane is low-entropy — and not in **repeated structures at region
granularity**. The first is what an entropy coder exploits. The second is the
only thing a base-plus-delta scheme can see. They are different properties of the
same bytes, and the weights have one and not the other.

---

## 2. Two checkpoints of the same model

From `reports/ccp_checkpoint_gpt2.txt`. A 16MB slice of real GPT-2 weights paired
with a derived variant, stored as one 32MB file. Storing one copy plus a free
delta would be 50%, so that is the ceiling.

| Divergence | bytes moved | CCP | CCP+gzip | gzip‑6 | LZMA‑6 | LZMA‑512MB |
| --- | --- | --- | --- | --- | --- | --- |
| identical | 0 | 49.99% | 53.41% | 6.82% | 7.52% | **53.75%** |
| 0.1% of weights replaced | 16 KB | 49.84% | 53.26% | 6.81% | 7.52% | **53.69%** |
| 1% replaced | 164 KB | 48.50% | 51.94% | 6.76% | 7.45% | **53.21%** |
| 10% replaced | 1.6 MB | 1.22% | 7.39% | 6.26% | 6.81% | **48.64%** |
| every weight ±1 ulp | 4.0 MB | −0.01% | 6.82% | 6.82% | 7.52% | 7.79% |
| every weight, 2 mantissa bytes | 8.0 MB | −0.01% | 6.79% | 6.79% | 7.30% | 7.55% |

Three things follow, and the second and third are the ones that matter.

**Cross-copy redundancy is real and large.** Half the bytes of a checkpoint pair
are recoverable, and gzip cannot see any of it — 6.82% — because its match window
is 32KB and the duplicate sits 16MB away. Window size, not algorithm family, is
what decides this.

**It is not exclusive to CCP.** LZMA with a 512MB dictionary reaches the
duplicate too, and beats CCP outright on every row, because it also compresses
the residual that CCP leaves alone. Any claim of a unique capability here is
false.

**The failure boundary is much sharper than LZMA's.** At 10% divergence LZMA
still returns 48.64% while CCP collapses to 1.22%. This is structural, not a
tuning miss: a delta pays a fixed ~3 bytes for every changed byte, so cost grows
linearly with the number of changes and crosses break-even at roughly a third of
the region, whereas a match encoder pays once per matched *run* and degrades
gracefully. CCP's usable band is somewhere between 1% and 10% sparse divergence.

### What actually moves is bytes, not weights

The last two rows are the most important in the study. A change that moves
**every** weight by one unit in the last place — the smallest representable
change, far smaller in weight space than replacing 1% of weights outright —
destroys the method completely (−0.01%), while the sparse 1% change barely dents
it (48.50%). A byte-level delta cannot see magnitude, only position. Ordinary
gradient-descent fine-tuning perturbs essentially every parameter slightly, which
is exactly the shape of update in those two rows. Adapter merges, targeted
edits, and partial-tensor updates are the favourable shape.

---

## 3. Cost

Same run, 32MB pair. Encode and decode measured separately, decode verified to
have produced the full byte count.

| | CCP | CCP+gzip | gzip‑6 | LZMA‑6 | LZMA‑512MB |
| --- | --- | --- | --- | --- | --- |
| encode | **0.54s** | 1.26s | 1.52s | 21.67s | 14.74s |
| decode | **0.18s** | 0.27s | 0.19s | 2.32s | 1.17s |
| saving | 49.99% | 53.41% | 6.82% | 7.52% | 53.75% |

This is the whole commercial argument, and it is narrow but real: **CCP+gzip
matches large-dictionary LZMA to within 0.34 points at ~12× the encode speed and
~4× the decode speed.** Decode is the side that matters for a model-loading path,
because it is paid on every load rather than once at publish time.

Throughput on larger real files, from the benchmark's performance table:

| File | encode | decode | peak RSS |
| --- | --- | --- | --- |
| distilbert-fp32.safetensors (255 MB) | 404 MB/s | 87 MB/s | 499 MB |
| gpt2-fp32.safetensors (523 MB) | 52 MB/s | 102 MB/s | 970 MB |
| qwen2.5-0.5b-q8_0.gguf (644 MB) | 162 MB/s | 72 MB/s | 1.17 GB |

Peak RSS runs to 1.86–1.95× file size. Most of that is mapped file pages rather
than private allocation — the input is memory-mapped and the kernel counts
resident mapped pages in the high-water mark — but it is reported as measured
rather than adjusted downward on that argument.

**A model cannot be used directly from this format.** The weights must be decoded
before load. The format does permit per-region random access: any region is
recoverable from its own record plus at most one base region, so a partial or
lazy decode is possible in principle. That was not built or measured, and should
not be counted as a result.

---

## 4. Where it works and where it does not

All 14 datasets round-tripped: **14/14 verified by SHA-256**.

Works:

| Dataset | CCP | best rival |
| --- | --- | --- |
| exact_repeats — one 64KB block repeated | 98.44% | LZMA 99.89% |
| zeros | 98.44% | bzip2 100.00% |
| near_duplicate_64k | 91.29% | LZMA 97.45% |
| near_duplicate_4k | 90.81% | LZMA 97.35% |
| sparse_weights — 5% non-zero | 78.94% | bzip2 95.40% |

Does not work:

| Dataset | CCP | gzip‑6 |
| --- | --- | --- |
| random_control (negative control) | −0.00% | −0.03% |
| fp32_weights (simulated dense) | −0.00% | 7.24% |
| int8_weights (simulated quantized) | −0.00% | 16.89% |
| structured_records (ordinary data) | −0.00% | **78.32%** |
| bert-tiny, distilbert (real) | −0.00% | 7.4–7.8% |

Two of these deserve emphasis because they bound the method's scope:

* **The negative control behaves.** Incompressible data yields −0.00%, i.e. the
  container costs slightly more than the input. A method that appeared to
  compress random data would be reporting a bug.
* **`structured_records` is the sharpest failure: 0.00% against gzip's 78.32%.**
  This data is enormously redundant — fixed-layout records with repeating fields
  — but the redundancy lives *inside* each region, and CCP only ever compares
  whole regions against each other. It is blind to everything below its region
  granularity. This is not a corner case; most ordinary binary data looks like
  this.

Notably, in the two cases built to favour a delta scheme, LZMA still wins:
97.45% vs 91.29% on 64KB near-duplicates, and 41.90% vs 12.95% on the drifting
set. On ratio alone, there is no dataset in this study where CCP is the best
available choice.

---

## 5. Region size matters, and not monotonically

Best region size is a property of the data, which is why the engine sweeps six
sizes instead of assuming one:

| Dataset | 4KB | 64KB | 1MB | 4MB |
| --- | --- | --- | --- | --- |
| exact_repeats | 6.02% | 6.14% | **98.44%** | 93.75% |
| sparse_weights | 63.73% | 24.54% | **78.94%** | 75.25% |
| gpt2-fp32 | **5.99%** | 2.44% | −0.00% | 0.57% |
| near_duplicate_4k | 20.89% | 8.18% | **90.81%** | 86.50% |

The spread is enormous — `exact_repeats` moves from 6% to 98% between 64KB and
1MB — and the optimum is not in the same place for different data. Any deployment
would have to sweep per file. `sparse_weights` is not even unimodal.

---

## 6. Answers to the questions that were asked

**Does CCP save real storage?** On a single model checkpoint, no — under 0.1% on
three of four real files, and the fourth is explained by constant mask buffers,
not weights. On collections of near-identical checkpoints, yes: up to 49.99%
alone, 53.41% with gzip over the residual.

**How much?** Bounded by 50% for a pair, and by `(1 − 1/n)` for *n* copies of one
base, minus what the deltas and index cost. Reached only while divergence stays
in the low single-digit percent of bytes.

**On what kind of data?** Long-range exact or near-exact duplication at region
granularity. Not fp32 weight distributions. Not data whose redundancy is
sub-region.

**What is the performance cost?** On the real files, encode 52–442 MB/s and decode
72–102 MB/s, peak RSS ~1.9× file size, and a mandatory decode before the model
can be loaded.

**Is there an advantage no existing method has?** Not on compression ratio — that
claim does not survive the LZMA-512MB column. On speed at equivalent ratio, yes:
~12× encode and ~4× decode against the compressor that matches it on size.

---

## 7. What would have to be true for this to matter commercially

The measured results support exactly one deployment shape: **a store holding many
near-identical checkpoints**, where the same base is shared across model
versions, and where publish-time and load-time CPU is worth more than the last
few percent of size. A fine-tune registry or a per-customer adapter fleet fits.
A single-model download does not.

Before that could be taken to anyone, four things in this study are load-bearing
and unfinished:

1. **The comparison is against the wrong opponent.** Content-addressed storage
   and deduplicating filesystems solve cross-checkpoint duplication as their
   primary job, and were not measured here at all. `zstd --long`, which is the
   actual production choice for this shape of data, was unavailable in this
   environment (no stdlib binding) and is missing from every table. LZMA was used
   as the stand-in. That gap has to be closed before any speed claim is made
   externally.
2. **Fixed-size offset-aligned regions are a real limitation.** A repeated
   structure shifted by even one byte is invisible. Content-defined chunking is
   the standard fix and is not implemented, so the measured savings are a lower
   bound of unknown looseness.
3. **The dense-update case is the common case.** Real fine-tuning moves nearly
   every byte. Unless the intended workload is adapter-style or partial updates,
   the favourable band in section 2 may rarely be occupied in practice.
4. **Only four real models were tested**, all small, and none above 1 GB of
   learned parameters. The scale behaviour in `reports/ccp_scale_1.4gb.txt`
   extends this to 1.4 GB but does not establish anything about the 100 GB+ range
   where storage cost actually becomes a line item.
