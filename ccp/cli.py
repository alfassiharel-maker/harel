"""Command-line access to the CCP Core.

This is a thin transport over `ccp.core` and `ccp.capabilities` -- it parses
arguments, calls the Core and prints what the Core measured. No algorithm lives
here, and no number printed here is computed here.

    python3 -m ccp.cli build <dir> --out model.ccp
    python3 -m ccp.cli stat model.ccp
    python3 -m ccp.cli extract model.ccp <uid> --out file
    python3 -m ccp.cli read model.ccp <uid> --offset 1024 --length 256
    python3 -m ccp.cli verify model.ccp
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional, Sequence

from .capabilities import CCPReader
from .core import (
    BuildConfig,
    CCPFormatError,
    CCPIntegrityError,
    DirectoryUnitSource,
    build_model,
    container_overhead,
    deserialize,
    serialize,
)


def _format_bytes(count: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(count) < 1024 or unit == "GB":
            return f"{count:.2f} {unit}" if unit != "B" else f"{int(count)} B"
        count /= 1024
    return f"{count:.2f} GB"


def _load(path: str):
    with open(path, "rb") as handle:
        return deserialize(handle.read())


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
    model = build_model(DirectoryUnitSource(args.directory), config)
    container = serialize(model)
    with open(args.out, "wb") as handle:
        handle.write(container)

    # Reconstruct everything before reporting a saving. A representation that has
    # not been shown to give back its input has not been shown to be one.
    for uid in model.uids():
        model.materialize(uid)

    parts = container_overhead(model, container)
    original = model.stats.original_bytes
    print(f"units            {model.stats.units}")
    print(f"  stored in full {model.stats.literals}")
    print(f"  stored as delta{model.stats.derived:>4}")
    print(f"original         {_format_bytes(original)}")
    print(f"container        {_format_bytes(parts['container_bytes'])}")
    print(f"  payload        {_format_bytes(parts['payload_bytes'])}")
    print(f"  index          {_format_bytes(parts['index_bytes'])}")
    if original:
        # Measured container length against measured input. Index included.
        saving = 1.0 - parts["container_bytes"] / original
        print(f"saving           {saving * 100:.2f}%")
    else:
        print("saving           n/a (no input bytes)")
    print(f"copied bytes     {_format_bytes(model.stats.copied_bytes)}")
    print(f"added bytes      {_format_bytes(model.stats.added_bytes)}")
    print("all units reconstructed and verified")
    return 0


def _cmd_stat(args: argparse.Namespace) -> int:
    model = _load(args.container)
    reader = CCPReader(model)
    print(f"{'unit':40} {'kind':8} {'size':>12} {'stored':>12} {'reuse':>7}")
    for uid in model.uids():
        report = reader.reuse_report(uid)
        ratio = report["reuse_ratio"]
        ratio_text = "     -" if ratio is None else f"{float(ratio) * 100:5.1f}%"
        print(
            f"{uid[:40]:40} {str(report['kind']):8} "
            f"{int(report['size']):>12} {int(report['encoded_bytes']):>12} {ratio_text:>7}"
        )
    groups = reader.shared_base_groups()
    if groups:
        print()
        print("shared-base groups:")
        for base_uid, members in sorted(groups.items()):
            print(f"  {base_uid} <- {len(members)} unit(s)")
    return 0


def _cmd_extract(args: argparse.Namespace) -> int:
    model = _load(args.container)
    data = model.materialize(args.uid)
    if args.out:
        with open(args.out, "wb") as handle:
            handle.write(data)
        print(f"wrote {len(data)} bytes to {args.out}")
    else:
        sys.stdout.buffer.write(data)
    return 0


def _cmd_read(args: argparse.Namespace) -> int:
    model = _load(args.container)
    result = CCPReader(model).read_range(args.uid, args.offset, args.length)
    ratio = result.work_ratio
    print(
        f"read {len(result.data)} bytes from {args.uid} "
        f"[{args.offset}:{args.offset + args.length}]",
        file=sys.stderr,
    )
    print(
        f"bytes touched {result.bytes_touched} of {result.unit_size} "
        f"({'n/a' if ratio is None else f'{ratio * 100:.2f}%'}); "
        f"instructions {result.instructions_visited}/{result.instructions_total}",
        file=sys.stderr,
    )
    if args.out:
        with open(args.out, "wb") as handle:
            handle.write(result.data)
    else:
        sys.stdout.buffer.write(result.data)
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    model = _load(args.container)
    failures: List[str] = []
    for uid in model.uids():
        try:
            model.materialize(uid)
        except (CCPIntegrityError, CCPFormatError) as error:
            failures.append(f"{uid}: {error}")
    if failures:
        for line in failures:
            print(line, file=sys.stderr)
        print(f"FAILED {len(failures)}/{len(model)} units", file=sys.stderr)
        return 1
    print(f"OK {len(model)} units reconstructed and verified")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ccp", description="CCP Core")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="build a CCP container from a directory")
    build.add_argument("directory")
    build.add_argument("--out", required=True)
    build.add_argument("--min-chunk", type=int, default=BuildConfig().min_chunk)
    build.add_argument("--avg-chunk", type=int, default=BuildConfig().avg_chunk)
    build.add_argument("--max-chunk", type=int, default=BuildConfig().max_chunk)
    build.add_argument(
        "--min-similarity", type=float, default=BuildConfig().min_similarity
    )
    build.set_defaults(func=_cmd_build)

    stat = sub.add_parser("stat", help="per-unit representation breakdown")
    stat.add_argument("container")
    stat.set_defaults(func=_cmd_stat)

    extract = sub.add_parser("extract", help="materialise one unit")
    extract.add_argument("container")
    extract.add_argument("uid")
    extract.add_argument("--out")
    extract.set_defaults(func=_cmd_extract)

    read = sub.add_parser("read", help="partial read, reporting the work it cost")
    read.add_argument("container")
    read.add_argument("uid")
    read.add_argument("--offset", type=int, default=0)
    read.add_argument("--length", type=int, default=256)
    read.add_argument("--out")
    read.set_defaults(func=_cmd_read)

    verify = sub.add_parser("verify", help="reconstruct and verify every unit")
    verify.add_argument("container")
    verify.set_defaults(func=_cmd_verify)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except BrokenPipeError:
        # `ccp stat ... | head` closes the pipe early. That is ordinary usage,
        # not an error, but Python would otherwise also complain at shutdown
        # when it flushes stdout, so stdout is redirected away first.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    except (CCPFormatError, CCPIntegrityError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
