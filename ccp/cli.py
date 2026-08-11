"""Command-line surface for CCP.

Transport only. It parses arguments, calls `ccp.api`, and prints what the Runtime
measured. No algorithm lives here and no number printed here is computed here.

It goes through the public API rather than reaching into `ccp.core` directly, so
the CLI is a consumer of the same interface an external integration would use --
if the API is not sufficient for the CLI, it is not sufficient for anyone.

    python3 -m ccp.cli build <dir> --out model.ccp
    python3 -m ccp.cli info model.ccp
    python3 -m ccp.cli stat model.ccp
    python3 -m ccp.cli verify model.ccp
    python3 -m ccp.cli extract model.ccp <uid> --out file
    python3 -m ccp.cli read model.ccp <uid> --offset 8000 --length 256
    python3 -m ccp.cli contract
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional, Sequence

from .api import (
    BuildConfig,
    CCPFormatError,
    CCPIntegrityError,
    UnknownUnitError,
    build,
    describe_contract,
    open_representation,
)


def _format_bytes(count: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(count) < 1024 or unit == "GB":
            return f"{int(count)} B" if unit == "B" else f"{count:.2f} {unit}"
        count /= 1024
    return f"{count:.2f} GB"


def _print_info(runtime) -> None:
    info = runtime.info()
    print(f"units            {info.units}")
    print(f"  stored in full {info.literals}")
    print(f"  stored as delta {info.derived}")
    print(f"original         {_format_bytes(info.original_bytes)}")
    print(f"representation   {_format_bytes(info.container_bytes)}")
    print(f"  payload        {_format_bytes(info.payload_bytes)}")
    print(f"  index          {_format_bytes(info.index_bytes)}")
    saving = info.saving
    print(
        "saving           "
        + ("n/a (no input bytes)" if saving is None else f"{saving * 100:.2f}%")
    )


def _cmd_build(args: argparse.Namespace) -> int:
    if not os.path.isdir(args.directory):
        print(f"not a directory: {args.directory}", file=sys.stderr)
        return 2
    config = BuildConfig(
        min_chunk=args.min_chunk,
        avg_chunk=args.avg_chunk,
        max_chunk=args.max_chunk,
        min_similarity=args.min_similarity,
    )
    container = build.from_directory(args.directory, config)
    with open(args.out, "wb") as handle:
        handle.write(container)

    # Reconstruct everything before reporting anything. A representation that has
    # not been shown to give back its input has not been shown to be one.
    runtime = open_representation(container)
    report = runtime.verify()
    _print_info(runtime)
    if not report.ok:
        print(f"VERIFICATION FAILED on {len(report.failures)} unit(s)", file=sys.stderr)
        for uid, error in report.failures[:10]:
            print(f"  {uid}: {error}", file=sys.stderr)
        return 1
    print(f"verified         {report.units_ok}/{report.units_checked} units")
    return 0


def _cmd_info(args: argparse.Namespace) -> int:
    _print_info(open_representation(args.container))
    return 0


def _cmd_stat(args: argparse.Namespace) -> int:
    runtime = open_representation(args.container)
    print(f"{'unit':44} {'kind':8} {'size':>10} {'stored':>10} {'instr':>6} {'reuse':>7}")
    for uid in runtime.units():
        unit = runtime.stat(uid)
        reuse = "     -" if unit.reuse_ratio is None else f"{unit.reuse_ratio * 100:5.1f}%"
        print(
            f"{uid[:44]:44} {unit.kind:8} {unit.size:>10} "
            f"{unit.stored_bytes:>10} {unit.instructions:>6} {reuse:>7}"
        )
    groups = runtime.groups()
    if groups:
        print()
        print("shared-base groups:")
        for base_uid, members in sorted(groups.items()):
            print(f"  {base_uid} <- {len(members)} unit(s)")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    runtime = open_representation(args.container)
    report = runtime.verify()
    if not report.ok:
        for uid, error in report.failures:
            print(f"{uid}: {error}", file=sys.stderr)
        print(
            f"FAILED {len(report.failures)}/{report.units_checked} units",
            file=sys.stderr,
        )
        return 1
    print(
        f"OK {report.units_ok} units, {_format_bytes(report.bytes_verified)} verified "
        f"in {report.elapsed_seconds * 1000:.1f} ms"
    )
    return 0


def _cmd_extract(args: argparse.Namespace) -> int:
    runtime = open_representation(args.container)
    data = runtime.materialize(args.uid)
    if args.out:
        with open(args.out, "wb") as handle:
            handle.write(data)
        print(f"wrote {len(data)} bytes to {args.out}")
    else:
        sys.stdout.buffer.write(data)
    return 0


def _cmd_read(args: argparse.Namespace) -> int:
    runtime = open_representation(args.container)
    result = runtime.read_range(args.uid, args.offset, args.length)
    ratio = result.work_ratio
    print(
        f"read {result.bytes_returned} bytes from {args.uid} "
        f"[{args.offset}:{args.offset + args.length}]",
        file=sys.stderr,
    )
    print(
        f"bytes touched {result.bytes_touched} of {result.unit_size} "
        f"({'n/a' if ratio is None else f'{ratio * 100:.2f}%'}); "
        f"instructions {result.instructions_visited}/{result.instructions_total}; "
        f"{result.elapsed_seconds * 1000:.3f} ms",
        file=sys.stderr,
    )
    if args.out:
        with open(args.out, "wb") as handle:
            handle.write(result.data)
    else:
        sys.stdout.buffer.write(result.data)
    return 0


def _cmd_contract(args: argparse.Namespace) -> int:
    print(describe_contract())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ccp", description="CCP")
    sub = parser.add_subparsers(dest="command", required=True)

    defaults = BuildConfig()
    cmd = sub.add_parser("build", help="build a representation from a directory")
    cmd.add_argument("directory")
    cmd.add_argument("--out", required=True)
    cmd.add_argument("--min-chunk", type=int, default=defaults.min_chunk)
    cmd.add_argument("--avg-chunk", type=int, default=defaults.avg_chunk)
    cmd.add_argument("--max-chunk", type=int, default=defaults.max_chunk)
    cmd.add_argument("--min-similarity", type=float, default=defaults.min_similarity)
    cmd.set_defaults(func=_cmd_build)

    cmd = sub.add_parser("info", help="sizes of a representation")
    cmd.add_argument("container")
    cmd.set_defaults(func=_cmd_info)

    cmd = sub.add_parser("stat", help="per-unit breakdown")
    cmd.add_argument("container")
    cmd.set_defaults(func=_cmd_stat)

    cmd = sub.add_parser("verify", help="reconstruct and verify every unit")
    cmd.add_argument("container")
    cmd.set_defaults(func=_cmd_verify)

    cmd = sub.add_parser("extract", help="materialise one unit")
    cmd.add_argument("container")
    cmd.add_argument("uid")
    cmd.add_argument("--out")
    cmd.set_defaults(func=_cmd_extract)

    cmd = sub.add_parser("read", help="selective read, reporting the work it cost")
    cmd.add_argument("container")
    cmd.add_argument("uid")
    cmd.add_argument("--offset", type=int, default=0)
    cmd.add_argument("--length", type=int, default=256)
    cmd.add_argument("--out")
    cmd.set_defaults(func=_cmd_read)

    cmd = sub.add_parser("contract", help="print the semantic contract")
    cmd.set_defaults(func=_cmd_contract)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except BrokenPipeError:
        # `ccp stat ... | head` closes the pipe early. Ordinary usage, not an
        # error, but stdout is redirected away so Python does not also complain
        # when it flushes at shutdown.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    except (CCPFormatError, CCPIntegrityError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except UnknownUnitError as error:
        print(f"error: unknown unit {error}", file=sys.stderr)
        return 2
    except (FileNotFoundError, NotADirectoryError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
