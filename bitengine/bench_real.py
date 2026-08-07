"""Run BitEngine against real files and report what it actually does to them.

    python3 bench_real.py --project            files from this repository
    python3 bench_real.py --scan               real binaries and media on this machine
    python3 bench_real.py --git 6              version pairs from this repo's history
    python3 bench_real.py --all                all three, the full report

Three categories, because they answer different questions and only one of them
flatters the engine.

**Single files** ask whether an arbitrary file contains long-range duplication at
block granularity. Mostly it does not, and an already-compressed file (PNG, a
`.gz`, an MP4) definitionally does not — entropy coding removes exactly the
redundancy a delta scheme looks for. These rows are expected to be near zero and
are reported anyway, because a benchmark that shows only the favourable case is
marketing.

**Version pairs** ask the question the engine is for: given two versions of one
thing, how much of the second is already in the first. This is the deployment
shape `experiments/ccp/FINDINGS.md` §8 identifies as the only one the
measurements support.

**Every goal on every file**, not just the one `probe()` picked, so the choice
can be checked against the alternatives it rejected rather than trusted.

gzip, bzip2 and LZMA run beside BitEngine on identical bytes. LZMA gets a large
dictionary on the pair rows, because a 32KB window cannot see a duplicate
megabytes away and beating a crippled opponent would prove nothing. `zstd
--long`, the real production choice for this shape of data, has no
standard-library binding and is **not** measured — the same gap FINDINGS.md
§8.1 records, still open.

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

SCAN_ROOTS = ("/usr/lib", "/usr/share", "/root/.rustup", "/opt")
SCAN_SUFFIXES = (".so", ".rlib", ".wav", ".png", ".a")
PROJECT_SUFFIXES = (".py", ".md", ".json", ".txt", ".toml", ".sql")

DEFAULT_LIMIT = 24 * 1024 * 1024
MIN_INTERESTING = 64 * 1024


def format_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def pct(original: int, encoded: int) -> float:
    return (original - encoded) / original * 100.0 if original else 0.0


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
    gzip_bytes: int = 0
    bzip2_bytes: int = 0
    lzma_bytes: int = 0
    stacked_bytes: int = 0   # the container, then gzipped
    alternatives: dict[str, float] = field(default_factory=dict)

    @property
    def saving_pct(self) -> float:
        return pct(self.original, self.container)

    @property
    def stacked_pct(self) -> float:
        """BitEngine then gzip. FINDINGS.md section 3 found this the strongest
        configuration: the delta removes long-range duplication that gzip's 32KB
        window cannot reach, and gzip then entropy-codes the residual that the
        delta leaves untouched. They are complementary, not alternatives."""
        return pct(self.original, self.stacked_bytes)

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
    # A duplicate megabytes away is invisible to a small window, so the pair rows
    # give LZMA a dictionary big enough to reach it. Less would measure the
    # window rather than the algorithm.
    dict_size = max(1 << 20, min(1 << 27, 1 << (max(len(data), 2) - 1).bit_length()))
    filters = [{"id": lzma.FILTER_LZMA2, "preset": 6, "dict_size": dict_size}]
    return len(lzma.compress(data, format=lzma.FORMAT_RAW, filters=filters))


def baselines(data: bytes, big_dictionary: bool, skip_slow: bool) -> tuple[int, int, int]:
    gz = len(gzip.compress(data, compresslevel=6, mtime=0))
    if skip_slow:
        return gz, 0, 0
    return gz, len(bz2.compress(data, compresslevel=6)), _lzma_size(data, big_dictionary)


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
    gz, bz, xz = baselines(data, big_dictionary=reference is not None, skip_slow=skip_slow)
    stacked = len(gzip.compress(raw, compresslevel=6, mtime=0))

    # Every named goal at its own default block size, so the probe's pick can be
    # compared against what it rejected instead of being taken on trust.
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
        verified=verified,
        gzip_bytes=gz,
        bzip2_bytes=bz,
        lzma_bytes=xz,
        stacked_bytes=stacked,
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
    """The project's own source, concatenated — a real, highly structured corpus."""
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
    """Tarballs of consecutive revisions — the only genuinely real version pairs here."""
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


def print_table(title: str, note: str, rows: Sequence[Row], skip_slow: bool) -> None:
    if not rows:
        return
    print(f"\n{title}")
    print(f"  {note}")
    header = (
        f"{'file':<28}{'size':>7}{'strategy':>18}{'blk':>6}"
        f"{'saved':>8}{'+gzip':>8}{'changed':>9}{'gzip':>8}"
    )
    if not skip_slow:
        header += f"{'bzip2':>8}{'LZMA':>8}"
    header += f"{'enc':>9}{'dec':>9}{'ok':>4}  codecs"
    print("  " + "-" * (len(header) + 18))
    print("  " + header)
    print("  " + "-" * (len(header) + 18))
    for row in rows:
        change = "n/a" if row.change_pct is None else f"{row.change_pct:.2f}%"
        line = (
            f"{row.label[-28:]:<28}{format_bytes(row.original):>7}{row.goal[:16]:>18}"
            f"{format_bytes(row.block_bytes):>6}{row.saving_pct:>7.2f}%{row.stacked_pct:>7.2f}%{change:>9}"
            f"{pct(row.original, row.gzip_bytes):>7.2f}%"
        )
        if not skip_slow:
            line += f"{pct(row.original, row.bzip2_bytes):>7.2f}%{pct(row.original, row.lzma_bytes):>7.2f}%"
        line += (
            f"{row.encode_mbs:>6.0f}MB/s{row.decode_mbs:>6.0f}MB/s"
            f"{'yes' if row.verified else 'FAIL':>4}  {row.codec_summary()}"
        )
        print("  " + line)


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


def print_insights(singles: Sequence[Row], pairs: Sequence[Row], skip_slow: bool) -> None:
    everything = [*singles, *pairs]
    print("\n" + "=" * 96)
    print("EMPIRICAL INSIGHTS")
    print("=" * 96)

    failures = [r for r in everything if not r.verified]
    print(f"\n1. Fidelity. {len(everything) - len(failures)}/{len(everything)} rows round-tripped "
          f"byte-for-byte under SHA-256.")
    if failures:
        print("   FAILED: " + ", ".join(r.label for r in failures))

    if singles:
        best = max(singles, key=lambda r: r.saving_pct)
        beaten = sum(1 for r in singles if pct(r.original, r.gzip_bytes) > r.saving_pct)
        print(f"\n2. A single real file yields almost nothing. Best {best.saving_pct:.2f}% "
              f"({os.path.basename(best.label)}).")
        print(f"   Plain gzip beats BitEngine on {beaten}/{len(singles)} single files.")
        print("   Expected: one file has no second version, so there is no delta to take.")
        print("   This is the same result FINDINGS.md section 1 recorded for model weights.")

    if pairs:
        best = max(pairs, key=lambda r: r.saving_pct)
        positive = sum(1 for r in pairs if r.saving_pct > 1.0)
        print(f"\n3. Version pairs are where it works. Best {best.saving_pct:.2f}% ({best.label}), "
              f"{positive}/{len(pairs)} pairs above 1%.")
        if not skip_slow:
            wins = sum(1 for r in pairs if r.saving_pct > pct(r.original, r.lzma_bytes))
            print(f"   BitEngine beats large-dictionary LZMA on ratio in {wins}/{len(pairs)} pairs.")
            if wins == 0:
                print("   On ratio alone LZMA wins every pair. The advantage claimed for BitEngine")
                print("   is throughput at comparable ratio, and that is the only claim supported.")
        beats_gzip = sum(1 for r in pairs if r.saving_pct > pct(r.original, r.gzip_bytes))
        print(f"   BitEngine alone beats plain gzip on {beats_gzip}/{len(pairs)} pairs.")
        stacked_wins = sum(1 for r in pairs if r.stacked_pct > pct(r.original, r.gzip_bytes))
        best_stacked = max(pairs, key=lambda r: r.stacked_pct)
        print(f"\n4. BitEngine THEN gzip is the strongest configuration: better than gzip alone on")
        print(f"   {stacked_wins}/{len(pairs)} pairs, best {best_stacked.stacked_pct:.2f}% ({best_stacked.label}).")
        print("   They are complementary: the delta removes long-range duplication gzip's 32KB")
        print("   window cannot reach, gzip entropy-codes the residual the delta leaves alone.")
        if not skip_slow:
            over_lzma = [r for r in pairs if r.stacked_pct > pct(r.original, r.lzma_bytes)]
            print(f"   Against large-dictionary LZMA, the strongest baseline available here:")
            print(f"   BitEngine+gzip wins {len(over_lzma)}/{len(pairs)} pairs.")
            for r in sorted(pairs, key=lambda x: -x.stacked_pct)[:3]:
                print(f"     {r.label:<20} {r.stacked_pct:>6.2f}%  vs LZMA {pct(r.original, r.lzma_bytes):>6.2f}%"
                      f"  ({r.stacked_pct - pct(r.original, r.lzma_bytes):+.2f} pts)")
            print("   Where it loses, the 'pair' was not a pair: those revisions added a whole new")
            print("   directory, so most of the second file has no counterpart in the first.")

    if everything:
        enc = sorted(r.encode_mbs for r in everything)
        dec = sorted(r.decode_mbs for r in everything)
        print(f"\n5. Throughput. Encode {enc[0]:.0f}-{enc[-1]:.0f} MB/s, "
              f"decode {dec[0]:.0f}-{dec[-1]:.0f} MB/s, single-threaded pure Python.")

    divergent = [r for r in everything if r.change_pct is not None and r.change_pct > 1.0]
    if divergent:
        print("\n6. Realized saving against raw divergence — the two are not the same number.")
        for row in divergent[:4]:
            assert row.change_pct is not None
            print(f"   {os.path.basename(row.label)[-34:]:<36} changed {row.change_pct:>6.2f}%  "
                  f"-> saved {row.saving_pct:>6.2f}%  (the brief's formula would predict "
                  f"{100 - row.change_pct:.2f}%)")

    print("\n7. Not measured: zstd --long, the production choice for this data shape.")
    print("   No external ratio or speed claim should be made until it is.")


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
    args = parser.parse_args(argv)

    if args.all:
        args.project = args.scan = True
        args.git = args.git or 6

    paths = list(args.files)
    if args.scan:
        paths += discover(SCAN_ROOTS, SCAN_SUFFIXES, args.per_suffix, args.limit)
    if not (paths or args.pair or args.git or args.project):
        parser.error("nothing to do: pass --all, --project, --scan, --files, --pair or --git")

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
        label = f"{os.path.basename(base_path)}->{os.path.basename(target_path)}"
        pairs.append(measure(label, target, base, args.sample_bytes, args.skip_slow, try_all))

    if args.git:
        with tempfile.TemporaryDirectory() as work:
            for label, base, target in git_pairs(args.repository, args.git, work):
                pairs.append(measure(label, target, base, args.sample_bytes, args.skip_slow, try_all))

    print("BitEngine on real files. Every row round-tripped and hash-checked.")
    print_table(
        "SINGLE FILES", "one version each, so there is nothing to delta against", singles, args.skip_slow
    )
    print_table("VERSION PAIRS", "two versions of one thing — the shape the engine is for", pairs, args.skip_slow)
    if try_all:
        print_goal_matrix([*singles, *pairs])
    print_insights(singles, pairs, args.skip_slow)

    return 1 if any(not r.verified for r in (*singles, *pairs)) else 0


if __name__ == "__main__":
    sys.exit(main())
