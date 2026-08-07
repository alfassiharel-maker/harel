"""BitEngine command line interface.

    python3 cli.py goals                        what the engine knows how to do
    python3 cli.py probe   SRC [--reference R]  measure candidate goals, rank them
    python3 cli.py pack    SRC OUT              write an optimised delta container
    python3 cli.py unpack  IN  OUT              restore the original bytes
    python3 cli.py inspect IN                   read a container's manifest

`pack` defaults to `--auto`, which probes a bounded prefix of the real file and
picks the goal and block size that measured best. That is the whole argument for
L2 existing: the same bytes returned −0.02% and 82.04% under two block sizes of
the same goal, so a default guessed from the file extension would be wrong more
often than it was right.

Every number printed is measured. `pack` verifies the container reconstructs to
the input's SHA-256 before reporting a saving, and exits non-zero if it does
not — a saving that cannot be reversed is not a saving.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

import l1
import l2
import l3

UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}

# Read for --auto. Enough to separate the candidates without the probe becoming
# a second full pass over a large file.
DEFAULT_SAMPLE_BYTES = 8 * 1024 * 1024

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_VERIFY_FAILED = 2


def parse_size(text: str) -> int:
    upper = text.strip().upper()
    for suffix, scale in sorted(UNITS.items(), key=lambda kv: -len(kv[0])):
        if upper.endswith(suffix):
            return int(float(upper[: -len(suffix)]) * scale)
    return int(upper)


def format_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} GB"


def format_pct(value: float | None) -> str:
    """`None` prints as `n/a`, never as 0.00%.

    An undefined saving and a measured saving of nothing are different facts and
    must not render identically.
    """
    return "n/a" if value is None else f"{value:.2f}%"


def _rule(width: int = 78) -> str:
    return "-" * width


# ---------------------------------------------------------------------------
# goals
# ---------------------------------------------------------------------------


def cmd_goals(args: argparse.Namespace) -> int:
    print("BitEngine optimisation goals\n")
    for goal in l2.GOALS.values():
        needs = " (needs --reference)" if goal.needs_reference else ""
        print(f"  {goal.name}{needs}")
        print(f"      {goal.summary}")
        print(
            f"      base={goal.base} block={format_bytes(goal.block_bytes)} "
            f"stride={goal.stride_blocks} codecs={','.join(sorted(l1.CODEC_NAMES[c] for c in goal.codecs))}"
        )
    print("\n  --auto probes the file and picks between these by measurement.")
    return EXIT_OK


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------


def read_prefix(path: str, limit: int) -> bytes:
    with open(path, "rb") as handle:
        return handle.read(limit)


def _print_probe_table(reports: Sequence[l2.GoalReport]) -> None:
    print(f"{'goal':>20} {'block':>10} {'saving':>9} {'blocks':>8}  codecs")
    print(_rule())
    for report in reports:
        codecs = ", ".join(f"{name}x{count}" for name, count in sorted(report.by_codec.items()))
        print(
            f"{report.goal.name:>20} {format_bytes(report.goal.block_bytes):>10} "
            f"{format_pct(report.saving_pct):>9} {report.blocks:>8}  {codecs or '-'}"
        )


def choose_goal(
    sample: bytes, reference_sample: bytes | None, candidates: Sequence[l2.Goal] | None = None
) -> tuple[l2.Goal, list[l2.GoalReport]]:
    reports = l2.probe(sample, candidates, reference_sample)
    measured = [r for r in reports if r.measured]
    if not measured:
        raise SystemExit(
            "probe could not measure any goal on this input — it is smaller than "
            f"{l2.MIN_PROBE_BLOCKS} blocks at every candidate block size. "
            "Pass --goal and --block-size explicitly."
        )
    return measured[0].goal, reports


def cmd_probe(args: argparse.Namespace) -> int:
    sample = read_prefix(args.source, args.sample_bytes)
    reference_sample = read_prefix(args.reference, args.sample_bytes) if args.reference else None
    if not sample:
        print(f"{args.source} is empty; nothing to probe.")
        return EXIT_OK

    reports = l2.probe(sample, None, reference_sample)
    measured = [r for r in reports if r.measured]

    print(f"Probed {format_bytes(len(sample))} of {args.source}")
    if not args.reference:
        print("No --reference given, so paired goals were not considered.")
    print()

    if not measured:
        print("No goal could be measured: the sample is smaller than two blocks everywhere.")
        return EXIT_OK

    _print_probe_table(measured[: args.top])
    best = measured[0]
    print(f"\nBest: {best.goal.name} at {format_bytes(best.goal.block_bytes)} "
          f"blocks, {format_pct(best.saving_pct)} on the sample.")
    print("Sample only. `pack` measures the whole file.")
    return EXIT_OK


# ---------------------------------------------------------------------------
# pack
# ---------------------------------------------------------------------------


def resolve_goal(args: argparse.Namespace) -> tuple[l2.Goal, list[l2.GoalReport]]:
    reports: list[l2.GoalReport] = []

    if args.goal:
        goal = l2.goal_named(args.goal)
    else:
        sample = read_prefix(args.source, args.sample_bytes)
        reference_sample = read_prefix(args.reference, args.sample_bytes) if args.reference else None
        goal, reports = choose_goal(sample, reference_sample)

    changes: dict[str, object] = {}
    if args.block_size:
        changes["block_bytes"] = parse_size(args.block_size)
    if args.stride is not None:
        changes["stride_blocks"] = args.stride
    if args.keyframe_interval is not None:
        changes["keyframe_interval"] = args.keyframe_interval
    if args.fast_decode:
        changes["codecs"] = l2.POLICY_FAST_DECODE
    if changes:
        goal = goal.replace(**changes)

    if goal.needs_reference and not args.reference:
        raise SystemExit(f"goal {goal.name!r} compares two streams; pass --reference")
    return goal, reports


def cmd_pack(args: argparse.Namespace) -> int:
    source_bytes = os.path.getsize(args.source)
    if source_bytes == 0:
        raise SystemExit(f"{args.source} is empty; nothing to pack")

    goal, reports = resolve_goal(args)

    if reports:
        print("Probe (sample):")
        _print_probe_table([r for r in reports if r.measured][: args.top])
        print()

    with open(args.source, "rb") as source, open(args.output, "wb") as destination:
        reference = open(args.reference, "rb") if args.reference else None  # noqa: SIM115
        try:
            manifest = l3.write_container(destination, source, goal, reference)
        finally:
            if reference is not None:
                reference.close()

    with l3.read_container(args.output, args.reference) as reader:
        verified = reader.verify()
        histogram = reader.codec_histogram()

    print(f"goal          {goal.name}")
    print(f"base          {goal.base}, stride {goal.stride_blocks}"
          + (f", keyframe every {goal.keyframe_interval}" if goal.keyframe_interval else ""))
    print(f"block size    {format_bytes(goal.block_bytes)}")
    print(f"blocks        {manifest.block_count}")
    print(_rule(46))
    print(f"input         {format_bytes(manifest.total_bytes):>14}")
    print(f"container     {format_bytes(manifest.container_bytes):>14}")
    print(f"saved         {format_bytes(manifest.total_bytes - manifest.container_bytes):>14}")
    print(f"saving        {format_pct(manifest.saving_pct):>14}")
    print(_rule(46))
    print("codecs        " + ", ".join(f"{n} x{c}" for n, c in sorted(histogram.items())))
    print(f"verify        {'PASS (sha256)' if verified else 'FAIL'}")

    if not verified:
        print("\nThe container does not reconstruct the input. It has been written but must not be trusted.",
              file=sys.stderr)
        return EXIT_VERIFY_FAILED
    return EXIT_OK


# ---------------------------------------------------------------------------
# unpack
# ---------------------------------------------------------------------------


def cmd_unpack(args: argparse.Namespace) -> int:
    with l3.read_container(args.container, args.reference) as reader:
        written = 0
        with open(args.output, "wb") as destination:
            for block in reader.blocks():
                destination.write(block)
                written += len(block)

        manifest = reader.manifest
        if written != manifest.total_bytes:
            print(
                f"restored {written} bytes but the manifest declares {manifest.total_bytes}",
                file=sys.stderr,
            )
            return EXIT_VERIFY_FAILED

        verified = reader.verify() if args.verify else None

    print(f"restored      {format_bytes(written)} to {args.output}")
    if verified is None:
        print("verify        skipped (--no-verify)")
        return EXIT_OK
    print(f"verify        {'PASS (sha256)' if verified else 'FAIL'}")
    if not verified:
        print("\nThe restored file does not match the recorded hash.", file=sys.stderr)
        return EXIT_VERIFY_FAILED
    return EXIT_OK


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------


def cmd_inspect(args: argparse.Namespace) -> int:
    with l3.read_container(args.container, args.reference) as reader:
        manifest = reader.manifest
        goal = manifest.goal
        histogram = reader.codec_histogram()
        # Reported for the tail block, which is the deepest, and before any
        # decoding so the cache cannot flatter the number.
        deepest = reader.chain_length(manifest.block_count - 1) if manifest.block_count else 0

    print(f"container     {args.container}")
    print(f"goal          {goal.name}")
    print(f"base          {goal.base}, stride {goal.stride_blocks}")
    print(f"keyframes     {goal.keyframe_interval or 'none'}")
    print(f"block size    {format_bytes(goal.block_bytes)}")
    print(f"blocks        {manifest.block_count}")
    print(f"original      {format_bytes(manifest.total_bytes)}")
    print(f"container     {format_bytes(manifest.container_bytes)}")
    print(f"saving        {format_pct(manifest.saving_pct)}")
    print(f"sha256        {manifest.sha256.hex()}")
    print("codecs        " + ", ".join(f"{n} x{c}" for n, c in sorted(histogram.items())))
    print(f"random access {deepest} block(s) to reach the last block")
    if deepest > 1 and not goal.keyframe_interval:
        print("              repack with --keyframe-interval N to bound this to N")
    return EXIT_OK


# ---------------------------------------------------------------------------
# wiring
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bitengine",
        description="Bit-level delta optimisation for files and streams.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_sample(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--sample-bytes",
            type=parse_size,
            default=DEFAULT_SAMPLE_BYTES,
            help="bytes read from the head of the file when probing",
        )
        p.add_argument("--top", type=int, default=8, help="rows of the probe table to print")

    goals = sub.add_parser("goals", help="list the built-in optimisation goals")
    goals.set_defaults(func=cmd_goals)

    probe = sub.add_parser("probe", help="measure candidate goals on a sample and rank them")
    probe.add_argument("source")
    probe.add_argument("--reference", help="second stream, for paired goals")
    add_sample(probe)
    probe.set_defaults(func=cmd_probe)

    pack = sub.add_parser("pack", help="write an optimised delta container")
    pack.add_argument("source")
    pack.add_argument("output")
    pack.add_argument("--goal", help="goal name; omit to choose by probing (--auto is the default)")
    pack.add_argument("--reference", help="second stream, for paired goals")
    pack.add_argument("--block-size", help="override the block size, e.g. 16KB")
    pack.add_argument("--stride", type=int, help="override PRECEDING stride, in blocks")
    pack.add_argument(
        "--keyframe-interval",
        type=int,
        help="store every Nth block standalone, bounding random-access cost",
    )
    pack.add_argument(
        "--fast-decode",
        action="store_true",
        help="bar the bitmap codec: smaller saving, predictable decode rate",
    )
    add_sample(pack)
    pack.set_defaults(func=cmd_pack)

    unpack = sub.add_parser("unpack", help="restore the original bytes from a container")
    unpack.add_argument("container")
    unpack.add_argument("output")
    unpack.add_argument("--reference", help="second stream, if the container needs one")
    unpack.add_argument(
        "--no-verify", dest="verify", action="store_false", help="skip the SHA-256 check"
    )
    unpack.set_defaults(func=cmd_unpack, verify=True)

    inspect = sub.add_parser("inspect", help="print a container's manifest without decoding it")
    inspect.add_argument("container")
    inspect.add_argument("--reference", help="second stream, if the container needs one")
    inspect.set_defaults(func=cmd_inspect)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = args.func(args)
        assert isinstance(result, int)
        return result
    except (l3.ContainerError, l1.CorruptBlock) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except (KeyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except FileNotFoundError as exc:
        print(f"error: {exc.filename}: not found", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
