"""Run BitEngine against real files and report what it actually does to them.

    python3 bench_real.py --project            files from this repository
    python3 bench_real.py --scan               real binaries and media on this machine
    python3 bench_real.py --git 6              version pairs from this repo's history
    python3 bench_real.py --all                all three, the full report

## Methodology, and a correction

An earlier version of this script compared BitEngine against gzip, bzip2 and
LZMA on the pair rows and reported that BitEngine won. That comparison was
wrong, and wrong in our favour: BitEngine was given **both** files, while the
baselines were given only the target. More information for one side is not a
result.

The rows are now split by what each method was allowed to see.

**Same-information comparisons — the fair ones.** Every method may use the
reference, and the question is: given version 1, how few bytes are needed to
reconstruct version 2?

    BitEngine            the container, which is a delta against the reference
    BitEngine + zstd     the container, then entropy-coded
    zstd --patch-from    zstd with the reference as a raw-content dictionary

`zstd --patch-from` is the production answer to this question and is the
opponent that matters. `experiments/ccp/FINDINGS.md` §8.1 records that it was
never measured and that no external claim should be made until it was. It is
measured here.

**Target-only comparisons — informational.** gzip, bzip2, LZMA and plain zstd
compressing the target alone, without the reference. They answer a different
question ("how compressible is this file on its own") and are printed for
context, never as the thing BitEngine beat.

**Single files** have no reference, so every method sees the same bytes and all
comparisons on those rows are fair. They are expected to be near zero for
BitEngine: one file has no second version, so there is no delta to take.

Every row is decoded and compared against its input by SHA-256. A row that does
not round-trip is reported as a failure, never omitted.
"""

from __future__ import annotations

import argparse
import bz2
import gzip
import hashlib
import io
import lzma
import os
import subprocess  # noqa: S404 — only `git archive` against a local repository
import sys
import tempfile
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field

import l2
import l3

try:
    import zstandard
except ImportError:  # pragma: no cover - exercised by the absence of the package
    zstandard = None  # type: ignore[assignment]

SCAN_ROOTS = ("/usr/lib", "/usr/share", "/root/.rustup", "/opt")
SCAN_SUFFIXES = (".so", ".rlib", ".wav", ".png", ".a")
PROJECT_SUFFIXES = (".py", ".md", ".json", ".txt", ".toml", ".sql")

DEFAULT_LIMIT = 24 * 1024 * 1024
MIN_INTERESTING = 64 * 1024

# zstd level 19 is its high-ratio setting, the fair counterpart to LZMA preset 6
# rather than to zstd's speed-oriented default of 3. Level 3 is measured too,
# because it is what a throughput comparison has to answer to.
ZSTD_HIGH = 19
ZSTD_FAST = 3
# 128MB window. The duplicate in a version pair sits a whole file away, which is
# past zstd's default window, and --long is exactly the flag that fixes that.
ZSTD_WINDOW_LOG = 27


def format_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def pct(original: int, encoded: int) -> float:
    return (original - encoded) / original * 100.0 if original else 0.0


def show(original: int, encoded: int) -> str:
    return "n/a" if encoded <= 0 else f"{pct(original, encoded):.2f}%"


@dataclass
class Row:
    label: str
    original: int
    container: int
    goal: str
    block_bytes: int
    codecs: dict[str, int]
    change_pct: float | None
    encode_mbs: float
    decode_mbs: float
    verified: bool
    has_reference: bool

    # target-only baselines
    gzip_bytes: int = 0
    bzip2_bytes: int = 0
    lzma_bytes: int = 0
    zstd_bytes: int = 0
    zstd_long_bytes: int = 0

    # same-information baselines (pairs only)
    zstd_patch_bytes: int = 0
    zstd_patch_encode_mbs: float = 0.0
    zstd_patch_decode_mbs: float = 0.0

    # BitEngine stacked with an entropy coder
    stacked_gzip_bytes: int = 0
    stacked_zstd_bytes: int = 0

    alternatives: dict[str, float] = field(default_factory=dict)

    @property
    def saving_pct(self) -> float:
        return pct(self.original, self.container)

    @property
    def stacked_pct(self) -> float:
        """BitEngine then zstd — the strongest configuration this engine has.

        The two are complementary rather than competing: the delta removes
        long-range duplication an entropy coder's window cannot reach, and the
        entropy coder codes the residual the delta leaves untouched.
        """
        return pct(self.original, self.stacked_zstd_bytes)

    @property
    def best_rival_bytes(self) -> int:
        """The smallest same-information rival, which for a pair is zstd patch-from."""
        return self.zstd_patch_bytes if self.has_reference else min(
            b for b in (self.gzip_bytes, self.lzma_bytes, self.zstd_long_bytes) if b > 0
        )

    def codec_summary(self) -> str:
        if not self.codecs:
            return "-"
        return " ".join(f"{name[:4]}:{count}" for name, count in sorted(self.codecs.items()))


# ---------------------------------------------------------------------------
# baselines
# ---------------------------------------------------------------------------


def _lzma_size(data: bytes, big_dictionary: bool) -> int:
    if not big_dictionary:
        return len(lzma.compress(data, preset=6))
    dict_size = max(1 << 20, min(1 << 27, 1 << (max(len(data), 2) - 1).bit_length()))
    filters = [{"id": lzma.FILTER_LZMA2, "preset": 6, "dict_size": dict_size}]
    return len(lzma.compress(data, format=lzma.FORMAT_RAW, filters=filters))


def _zstd_compressor(level: int, long_range: bool, dictionary: bytes | None = None):  # type: ignore[no-untyped-def]
    assert zstandard is not None
    kwargs: dict[str, object] = {}
    if dictionary is not None:
        # A raw-content dictionary is what `zstd --patch-from` uses: the
        # reference is addressable as history rather than trained into a
        # statistical model.
        kwargs["dict_data"] = zstandard.ZstdCompressionDict(
            dictionary, dict_type=zstandard.DICT_TYPE_RAWCONTENT
        )
    if long_range:
        kwargs["compression_params"] = zstandard.ZstdCompressionParameters.from_level(
            level, enable_ldm=True, window_log=ZSTD_WINDOW_LOG
        )
    else:
        kwargs["level"] = level
    return zstandard.ZstdCompressor(**kwargs)  # type: ignore[arg-type]


def zstd_size(data: bytes, level: int, long_range: bool) -> int:
    if zstandard is None:
        return 0
    return len(_zstd_compressor(level, long_range).compress(data))


def zstd_patch_from(target: bytes, reference: bytes) -> tuple[int, float, float, bool]:
    """`zstd --patch-from`: compress target with reference as history.

    Returns size, encode MB/s, decode MB/s and whether it round-tripped. This is
    the same job BitEngine does, so it is the only baseline whose ratio may be
    compared with ours directly.
    """
    if zstandard is None:
        return 0, 0.0, 0.0, False

    compressor = _zstd_compressor(ZSTD_HIGH, long_range=True, dictionary=reference)
    started = time.perf_counter()
    packed = compressor.compress(target)
    encode_seconds = time.perf_counter() - started

    dictionary = zstandard.ZstdCompressionDict(reference, dict_type=zstandard.DICT_TYPE_RAWCONTENT)
    decompressor = zstandard.ZstdDecompressor(
        dict_data=dictionary, max_window_size=1 << ZSTD_WINDOW_LOG
    )
    started = time.perf_counter()
    restored = decompressor.decompress(packed, max_output_size=len(target) * 2 + 1024)
    decode_seconds = time.perf_counter() - started

    megabytes = len(target) / 1e6
    return (
        len(packed),
        megabytes / encode_seconds if encode_seconds else 0.0,
        megabytes / decode_seconds if decode_seconds else 0.0,
        restored == target,
    )


# ---------------------------------------------------------------------------
# measurement
# ---------------------------------------------------------------------------


def run_goal(data: bytes, goal: l2.Goal, reference: bytes | None) -> tuple[l3.Manifest, bytes, float, float, bool]:
    container = io.BytesIO()
    started = time.perf_counter()
    manifest = l3.write_container(
        container, io.BytesIO(data), goal, io.BytesIO(reference) if reference is not None else None
    )
    encode_seconds = time.perf_counter() - started

    raw = container.getvalue()
    started = time.perf_counter()
    with l3.Reader(io.BytesIO(raw), io.BytesIO(reference) if reference is not None else None) as reader:
        restored = b"".join(reader.blocks())
    decode_seconds = time.perf_counter() - started

    verified = restored == data and hashlib.sha256(restored).digest() == manifest.sha256
    megabytes = len(data) / 1e6
    return (
        manifest,
        raw,
        megabytes / encode_seconds if encode_seconds else 0.0,
        megabytes / decode_seconds if decode_seconds else 0.0,
        verified,
    )


def measure(
    label: str,
    data: bytes,
    reference: bytes | None,
    sample_bytes: int,
    skip_slow: bool,
    try_all_goals: bool,
) -> Row:
    candidates = [l2.goal_named("checkpoint-pair")] if reference is not None else None
    reports = l2.probe(data[:sample_bytes], candidates, reference[:sample_bytes] if reference else None)
    measured = [r for r in reports if r.measured]
    goal = measured[0].goal if measured else l2.goal_named("anchor-dedup").with_block_bytes(4096)

    manifest, raw, encode_mbs, decode_mbs, verified = run_goal(data, goal, reference)

    gz = len(gzip.compress(data, compresslevel=6, mtime=0))
    bz = 0 if skip_slow else len(bz2.compress(data, compresslevel=6))
    xz = 0 if skip_slow else _lzma_size(data, big_dictionary=reference is not None)

    patch_bytes, patch_enc, patch_dec, patch_ok = (0, 0.0, 0.0, True)
    if reference is not None:
        patch_bytes, patch_enc, patch_dec, patch_ok = zstd_patch_from(data, reference)

    alternatives: dict[str, float] = {}
    if try_all_goals:
        for name, candidate in l2.GOALS.items():
            if candidate.needs_reference != (reference is not None):
                continue
            try:
                alt_manifest, alt_raw, _, _, alt_ok = run_goal(data, candidate, reference)
            except (ValueError, l3.ContainerError):
                continue
            alternatives[name] = pct(alt_manifest.total_bytes, len(alt_raw)) if alt_ok else float("nan")

    return Row(
        label=label,
        original=manifest.total_bytes,
        container=len(raw),
        goal=goal.name,
        block_bytes=goal.block_bytes,
        codecs=manifest.codec_counts or {},
        change_pct=manifest.block_savings.change_pct if manifest.block_savings else None,
        encode_mbs=encode_mbs,
        decode_mbs=decode_mbs,
        verified=verified and patch_ok,
        has_reference=reference is not None,
        gzip_bytes=gz,
        bzip2_bytes=bz,
        lzma_bytes=xz,
        zstd_bytes=zstd_size(data, ZSTD_HIGH, long_range=False),
        zstd_long_bytes=zstd_size(data, ZSTD_HIGH, long_range=True),
        zstd_patch_bytes=patch_bytes,
        zstd_patch_encode_mbs=patch_enc,
        zstd_patch_decode_mbs=patch_dec,
        stacked_gzip_bytes=len(gzip.compress(raw, compresslevel=6, mtime=0)),
        stacked_zstd_bytes=zstd_size(raw, ZSTD_HIGH, long_range=True) or len(gzip.compress(raw, 6, mtime=0)),
        alternatives=alternatives,
    )


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------


def discover(roots: Sequence[str], suffixes: Sequence[str], per_suffix: int, limit: int) -> list[str]:
    """Deterministic discovery: walked in sorted order, never sampled."""
    found: dict[str, list[str]] = {suffix: [] for suffix in suffixes}
    for root in roots:
        if not os.path.isdir(root):
            continue
        for directory, subdirs, names in os.walk(root, onerror=lambda _e: None):
            subdirs[:] = sorted(d for d in subdirs if not d.startswith("."))
            for name in sorted(names):
                suffix = os.path.splitext(name)[1]
                if suffix not in found or len(found[suffix]) >= per_suffix:
                    continue
                path = os.path.join(directory, name)
                try:
                    size = os.path.getsize(path)
                except OSError:
                    continue
                if MIN_INTERESTING <= size <= limit and os.path.isfile(path):
                    found[suffix].append(path)
            if all(len(v) >= per_suffix for v in found.values()):
                break
    return [path for paths in found.values() for path in paths]


def concatenated_project(root: str, suffixes: Sequence[str], limit: int) -> bytes:
    out = bytearray()
    for directory, subdirs, names in os.walk(root, onerror=lambda _e: None):
        subdirs[:] = sorted(d for d in subdirs if not d.startswith(".") and d != "node_modules")
        for name in sorted(names):
            if os.path.splitext(name)[1] not in suffixes:
                continue
            try:
                with open(os.path.join(directory, name), "rb") as handle:
                    out += handle.read()
            except OSError:
                continue
            if len(out) >= limit:
                return bytes(out[:limit])
    return bytes(out)


def git_pairs(repository: str, count: int, work: str) -> Iterator[tuple[str, bytes, bytes]]:
    revisions = subprocess.run(  # noqa: S603
        ["git", "-C", repository, "log", "--format=%H", f"-{count + 1}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    revisions.reverse()

    def archive(revision: str) -> bytes:
        path = os.path.join(work, f"{revision[:12]}.tar")
        with open(path, "wb") as handle:
            subprocess.run(  # noqa: S603
                ["git", "-C", repository, "archive", "--format=tar", revision], stdout=handle, check=True
            )
        with open(path, "rb") as handle:
            return handle.read()

    previous_revision = revisions[0]
    previous = archive(previous_revision)
    for revision in revisions[1:]:
        current = archive(revision)
        yield f"{previous_revision[:7]}..{revision[:7]}", previous, current
        previous, previous_revision = current, revision


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def print_pairs(rows: Sequence[Row]) -> None:
    if not rows:
        return
    print("\nVERSION PAIRS — SAME-INFORMATION COMPARISON")
    print("  Every method may use the reference. Given version 1, how few bytes reconstruct version 2?")
    header = (
        f"{'pair':<20}{'size':>7}{'changed':>9}"
        f"{'BitEngine':>11}{'BE+zstd':>10}{'zstd patch':>11}"
        f"{'BE enc':>9}{'BE dec':>9}{'zstd enc':>10}{'zstd dec':>10}{'ok':>4}"
    )
    print("  " + "-" * len(header))
    print("  " + header)
    print("  " + "-" * len(header))
    for row in rows:
        change = "n/a" if row.change_pct is None else f"{row.change_pct:.2f}%"
        print(
            "  "
            + f"{row.label[-20:]:<20}{format_bytes(row.original):>7}{change:>9}"
            + f"{row.saving_pct:>10.2f}%{row.stacked_pct:>9.2f}%"
            + f"{show(row.original, row.zstd_patch_bytes):>11}"
            + f"{row.encode_mbs:>6.0f}MB/s{row.decode_mbs:>6.0f}MB/s"
            + f"{row.zstd_patch_encode_mbs:>7.0f}MB/s{row.zstd_patch_decode_mbs:>7.0f}MB/s"
            + f"{'yes' if row.verified else 'FAIL':>4}"
        )

    print("\n  Target-only baselines (informational — these never saw the reference):")
    sub = f"{'pair':<20}{'gzip':>9}{'bzip2':>9}{'LZMA':>9}{'zstd':>9}{'zstd --long':>13}"
    print("  " + sub)
    for row in rows:
        print(
            "  "
            + f"{row.label[-20:]:<20}{show(row.original, row.gzip_bytes):>9}"
            + f"{show(row.original, row.bzip2_bytes):>9}{show(row.original, row.lzma_bytes):>9}"
            + f"{show(row.original, row.zstd_bytes):>9}{show(row.original, row.zstd_long_bytes):>13}"
        )


def print_singles(rows: Sequence[Row]) -> None:
    if not rows:
        return
    print("\nSINGLE FILES — one version each, so every method sees identical bytes")
    header = (
        f"{'file':<28}{'size':>7}{'strategy':>18}{'BitEngine':>11}{'BE+zstd':>10}"
        f"{'gzip':>9}{'LZMA':>9}{'zstd':>9}{'enc':>9}{'dec':>9}{'ok':>4}"
    )
    print("  " + "-" * len(header))
    print("  " + header)
    print("  " + "-" * len(header))
    for row in rows:
        print(
            "  "
            + f"{row.label[-28:]:<28}{format_bytes(row.original):>7}{row.goal[:16]:>18}"
            + f"{row.saving_pct:>10.2f}%{row.stacked_pct:>9.2f}%"
            + f"{show(row.original, row.gzip_bytes):>9}{show(row.original, row.lzma_bytes):>9}"
            + f"{show(row.original, row.zstd_bytes):>9}"
            + f"{row.encode_mbs:>6.0f}MB/s{row.decode_mbs:>6.0f}MB/s{'yes' if row.verified else 'FAIL':>4}"
        )


def print_goal_matrix(rows: Sequence[Row]) -> None:
    rows = [r for r in rows if r.alternatives]
    if not rows:
        return
    names = sorted({name for row in rows for name in row.alternatives})
    print("\nEVERY GOAL ON EVERY FILE — what the probe's pick was chosen against")
    header = f"{'file':<28}" + "".join(f"{n[:15]:>17}" for n in names) + f"{'probe picked':>20}"
    print("  " + "-" * len(header))
    print("  " + header)
    print("  " + "-" * len(header))
    for row in rows:
        cells = "".join(
            f"{row.alternatives[n]:>16.2f}%" if n in row.alternatives else f"{'-':>17}" for n in names
        )
        print("  " + f"{row.label[-28:]:<28}{cells}{row.goal[:18]:>20}")


def print_insights(singles: Sequence[Row], pairs: Sequence[Row]) -> None:
    everything = [*singles, *pairs]
    print("\n" + "=" * 100)
    print("EMPIRICAL INSIGHTS")
    print("=" * 100)

    failures = [r for r in everything if not r.verified]
    print(f"\n1. Fidelity. {len(everything) - len(failures)}/{len(everything)} rows round-tripped "
          "byte-for-byte under SHA-256.")
    if failures:
        print("   FAILED: " + ", ".join(r.label for r in failures))

    if singles:
        best = max(singles, key=lambda r: r.saving_pct)
        beaten = sum(1 for r in singles if r.gzip_bytes and pct(r.original, r.gzip_bytes) > r.saving_pct)
        print(f"\n2. A single real file yields almost nothing. Best {best.saving_pct:.2f}% "
              f"({os.path.basename(best.label)}).")
        print(f"   Plain gzip beats BitEngine on {beaten}/{len(singles)} single files.")
        print("   Expected: one file has no second version, so there is no delta to take.")
        print("   Same result FINDINGS.md section 1 recorded for model weights.")

    if pairs and zstandard is not None:
        be_wins = [r for r in pairs if r.container < r.zstd_patch_bytes]
        stacked_wins = [r for r in pairs if r.stacked_zstd_bytes < r.zstd_patch_bytes]
        print(f"\n3. Against zstd --patch-from, the same-information opponent that matters:")
        print(f"   BitEngine alone wins {len(be_wins)}/{len(pairs)} pairs.")
        print(f"   BitEngine + zstd wins {len(stacked_wins)}/{len(pairs)} pairs.")
        for row in sorted(pairs, key=lambda r: -r.stacked_pct)[:5]:
            delta = row.stacked_pct - pct(row.original, row.zstd_patch_bytes)
            print(f"     {row.label:<20} BE+zstd {row.stacked_pct:>6.2f}%  "
                  f"zstd patch {pct(row.original, row.zstd_patch_bytes):>6.2f}%  ({delta:+.2f} pts)")
        if not stacked_wins:
            print("   zstd --patch-from wins every pair on ratio. That is the honest headline and")
            print("   it removes the ratio claim entirely. What remains has to be argued on")
            print("   something else, or not argued at all.")

        enc_ratio = [r.zstd_patch_encode_mbs / r.encode_mbs for r in pairs if r.encode_mbs]
        dec_ratio = [r.zstd_patch_decode_mbs / r.decode_mbs for r in pairs if r.decode_mbs]
        if enc_ratio and dec_ratio:
            print(f"\n4. Throughput against the same opponent, both single-threaded:")
            print(f"   zstd encodes {min(enc_ratio):.1f}-{max(enc_ratio):.1f}x faster than BitEngine.")
            print(f"   zstd decodes {min(dec_ratio):.1f}-{max(dec_ratio):.1f}x faster than BitEngine.")
            print("   BitEngine is pure Python against a tuned C library, so this is expected —")
            print("   but it is measured, and it means there is no speed claim either, today.")

    divergent = [r for r in everything if r.change_pct is not None and r.change_pct > 1.0]
    if divergent:
        print("\n5. Realised saving against raw divergence — the brief's formula overstates both.")
        for row in divergent[:4]:
            assert row.change_pct is not None
            print(f"   {os.path.basename(row.label)[-30:]:<32} changed {row.change_pct:>6.2f}%  "
                  f"-> saved {row.saving_pct:>6.2f}%  (formula predicts {100 - row.change_pct:.2f}%)")

    if zstandard is None:
        print("\n6. zstd is NOT installed, so the comparison that matters did not run.")
        print("   pip install zstandard, then re-run. Do not quote this report without it.")


def head_to_head(reference: bytes, target: bytes, block_bytes: int = 65536) -> None:
    """The decisive comparison: BitEngine against zstd --patch-from at every level.

    Run separately from the main tables because it answers the only question a
    technical reviewer will actually ask — is there any operating point where
    this engine wins? Both methods get the same reference and must reconstruct
    the same target.

    The chunked row exists because "we support random access and zstd does not"
    is the obvious last claim, and chunking zstd per block is the obvious
    rebuttal. It is measured here rather than left for someone else to raise.
    """
    if zstandard is None:
        print("\nzstd is not installed; the decisive comparison cannot run.")
        return

    print(f"\nHEAD TO HEAD — {len(target) / 1e6:.2f} MB target against a {len(reference) / 1e6:.2f} MB reference")
    print(f"  {'method':<38}{'size':>11}{'saving':>9}{'encode':>11}{'decode':>11}{'random':>10}")
    print("  " + "-" * 90)

    for level in (1, 3, 9, 19):
        dictionary = zstandard.ZstdCompressionDict(reference, dict_type=zstandard.DICT_TYPE_RAWCONTENT)
        params = zstandard.ZstdCompressionParameters.from_level(
            level, enable_ldm=True, window_log=ZSTD_WINDOW_LOG
        )
        compressor = zstandard.ZstdCompressor(dict_data=dictionary, compression_params=params)
        started = time.perf_counter()
        packed = compressor.compress(target)
        encode_seconds = time.perf_counter() - started
        decompressor = zstandard.ZstdDecompressor(dict_data=dictionary, max_window_size=1 << ZSTD_WINDOW_LOG)
        started = time.perf_counter()
        restored = decompressor.decompress(packed, max_output_size=len(target) * 2 + 1024)
        decode_seconds = time.perf_counter() - started
        if restored != target:
            raise SystemExit("zstd patch-from did not round-trip")
        megabytes = len(target) / 1e6
        print(f"  {'zstd --patch-from -' + str(level):<38}{len(packed):>11,}{pct(len(target), len(packed)):>8.2f}%"
              f"{megabytes / encode_seconds:>8.0f}MB/s{megabytes / decode_seconds:>8.0f}MB/s{'whole':>10}")

    blocks = [target[i : i + block_bytes] for i in range(0, len(target), block_bytes)]
    references = [reference[i : i + block_bytes] for i in range(0, len(reference), block_bytes)]
    references += [b""] * (len(blocks) - len(references))

    chunks: list[bytes] = []
    started = time.perf_counter()
    for block, reference_block in zip(blocks, references, strict=True):
        dictionary = zstandard.ZstdCompressionDict(reference_block or b"\x00", dict_type=zstandard.DICT_TYPE_RAWCONTENT)
        chunks.append(zstandard.ZstdCompressor(level=3, dict_data=dictionary).compress(block))
    encode_seconds = time.perf_counter() - started
    total = sum(len(c) for c in chunks)

    probes = [len(blocks) - 1, len(blocks) // 2, min(3, len(blocks) - 1)]
    latencies = []
    for index in probes:
        dictionary = zstandard.ZstdCompressionDict(references[index] or b"\x00", dict_type=zstandard.DICT_TYPE_RAWCONTENT)
        started = time.perf_counter()
        out = zstandard.ZstdDecompressor(dict_data=dictionary).decompress(chunks[index], max_output_size=block_bytes * 2)
        latencies.append((time.perf_counter() - started) * 1000)
        if out != blocks[index]:
            raise SystemExit("chunked zstd did not round-trip")
    megabytes = len(target) / 1e6
    print(f"  {'chunked zstd --patch-from -3':<38}{total:>11,}{pct(len(target), total):>8.2f}%"
          f"{megabytes / encode_seconds:>8.0f}MB/s{'-':>12}{sorted(latencies)[len(latencies) // 2]:>7.2f}ms")

    goal = l2.goal_named("checkpoint-pair").with_block_bytes(block_bytes)
    manifest, raw, encode_mbs, decode_mbs, verified = run_goal(target, goal, reference)
    latencies = []
    for index in probes:
        reader = l3.Reader(io.BytesIO(raw), io.BytesIO(reference))
        started = time.perf_counter()
        block = reader.block(index)
        latencies.append((time.perf_counter() - started) * 1000)
        if block != blocks[index]:
            raise SystemExit("BitEngine random access did not match")
        reader.close()
    print(f"  {'BitEngine container':<38}{len(raw):>11,}{pct(len(target), len(raw)):>8.2f}%"
          f"{encode_mbs:>8.0f}MB/s{decode_mbs:>8.0f}MB/s{sorted(latencies)[len(latencies) // 2]:>7.2f}ms")
    stacked = zstd_size(raw, ZSTD_HIGH, long_range=True)
    print(f"  {'BitEngine + zstd':<38}{stacked:>11,}{pct(len(target), stacked):>8.2f}%"
          f"{'-':>12}{'-':>12}{'-':>10}")
    print(f"  verified: {'yes' if verified else 'FAIL'}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--all", action="store_true", help="project files, system files and git pairs")
    parser.add_argument("--project", action="store_true", help="files from this repository")
    parser.add_argument("--scan", action="store_true", help="real binaries and media on this machine")
    parser.add_argument("--files", nargs="*", default=[], help="explicit files to measure")
    parser.add_argument("--pair", nargs=2, action="append", default=[], metavar=("BASE", "TARGET"))
    parser.add_argument("--git", type=int, default=0, help="build N version pairs from git history")
    parser.add_argument("--repository", default="..", help="repository root for --git and --project")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="bytes read per file")
    parser.add_argument("--sample-bytes", type=int, default=4 * 1024 * 1024)
    parser.add_argument("--per-suffix", type=int, default=2)
    parser.add_argument("--skip-slow", action="store_true", help="drop the bzip2 and LZMA baselines")
    parser.add_argument("--no-goal-matrix", action="store_true", help="skip the every-goal comparison")
    parser.add_argument("--head-to-head", action="store_true",
                        help="BitEngine against zstd --patch-from at every level, on the newest git pair")
    args = parser.parse_args(argv)

    if args.all:
        args.project = args.scan = True
        args.git = args.git or 6

    paths = list(args.files)
    if args.scan:
        paths += discover(SCAN_ROOTS, SCAN_SUFFIXES, args.per_suffix, args.limit)
    if not (paths or args.pair or args.git or args.project or args.head_to_head):
        parser.error("nothing to do: pass --all, --project, --scan, --files, --pair, --git or --head-to-head")

    try_all = not args.no_goal_matrix
    singles: list[Row] = []

    if args.project:
        corpus = concatenated_project(args.repository, PROJECT_SUFFIXES, args.limit)
        if len(corpus) >= MIN_INTERESTING:
            singles.append(
                measure("project source (concatenated)", corpus, None, args.sample_bytes, args.skip_slow, try_all)
            )

    for path in paths:
        with open(path, "rb") as handle:
            data = handle.read(args.limit)
        if len(data) < MIN_INTERESTING:
            continue
        singles.append(measure(path, data, None, args.sample_bytes, args.skip_slow, try_all))

    pairs: list[Row] = []
    for base_path, target_path in args.pair:
        with open(base_path, "rb") as handle:
            base = handle.read(args.limit)
        with open(target_path, "rb") as handle:
            target = handle.read(args.limit)
        pairs.append(
            measure(
                f"{os.path.basename(base_path)}->{os.path.basename(target_path)}",
                target, base, args.sample_bytes, args.skip_slow, try_all,
            )
        )

    if args.git:
        with tempfile.TemporaryDirectory() as work:
            for label, base, target in git_pairs(args.repository, args.git, work):
                pairs.append(measure(label, target, base, args.sample_bytes, args.skip_slow, try_all))

    if args.head_to_head:
        with tempfile.TemporaryDirectory() as work:
            newest = list(git_pairs(args.repository, 1, work))[-1]
            head_to_head(newest[1], newest[2])
        return 0

    version = f"zstandard {zstandard.__version__}" if zstandard else "zstd NOT INSTALLED"
    print(f"BitEngine on real files. Every row round-tripped and hash-checked. ({version})")
    print_singles(singles)
    print_pairs(pairs)
    if try_all:
        print_goal_matrix([*singles, *pairs])
    print_insights(singles, pairs)

    return 1 if any(not r.verified for r in (*singles, *pairs)) else 0


if __name__ == "__main__":
    sys.exit(main())
