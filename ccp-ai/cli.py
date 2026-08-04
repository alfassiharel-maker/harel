#!/usr/bin/env python3
"""ccp — pack, rebuild and measure real checkpoints from the terminal.

The console is for the pitch; this is for the CTO who wants to run the engine
against their own weights before believing anything:

    python3 ccp-ai/cli.py pack   --base base.safetensors --variant ft.safetensors --out ft.ccp
    python3 ccp-ai/cli.py unpack --base base.safetensors --container ft.ccp --out rebuilt.safetensors
    python3 ccp-ai/cli.py inspect ft.ccp
    python3 ccp-ai/cli.py bench  --base base.safetensors --variant ft.safetensors
    python3 ccp-ai/cli.py demo

`pack` verifies before it writes and `unpack` verifies before it returns, so a
successful exit code is itself the lossless claim. Real `.safetensors` files from
any hub work — the engine reads byte spans and never interprets a dtype it does
not recognise.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import pack as packer  # noqa: E402
from engine import metrics, safetensors  # noqa: E402
from engine.codec import sha256  # noqa: E402


def _read(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def _write(path: str, blob: bytes) -> None:
    with open(path, "wb") as fh:
        fh.write(blob)


def cmd_pack(args: argparse.Namespace) -> int:
    base = _read(args.base)
    variant = _read(args.variant)
    started = time.perf_counter()
    container, stats = packer.pack(
        base,
        variant,
        base_id=os.path.basename(args.base),
        variant_id=os.path.basename(args.variant),
        compressor=args.compressor,
    )
    elapsed = time.perf_counter() - started
    _write(args.out, container)

    print(f"variant   {metrics.fmt_bytes(stats.variant_raw_bytes)}")
    print(f"container {metrics.fmt_bytes(stats.container_bytes)}")
    print(f"saving    {stats.savings_ratio:.1%}" if stats.savings_ratio is not None else "saving    —")
    print(
        f"tensors   {stats.tensors_identical} copied · {stats.tensors_delta} delta · "
        f"{stats.tensors_literal} whole"
    )
    print(f"encoded   {elapsed:.2f}s ({metrics.fmt_bytes(stats.variant_raw_bytes / elapsed)}/s)")
    print("verified  bit-exact (a container that fails verification is never written)")
    return 0


def cmd_unpack(args: argparse.Namespace) -> int:
    base = _read(args.base)
    container = _read(args.container)
    started = time.perf_counter()
    rebuilt = packer.unpack(container, base, verify=True)
    elapsed = time.perf_counter() - started
    _write(args.out, rebuilt)
    print(f"rebuilt   {metrics.fmt_bytes(len(rebuilt))} in {elapsed:.2f}s")
    print(f"sha256    {sha256(rebuilt)}")
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    detail = packer.describe(_read(args.container))
    print(f"base      {detail['base']['id']}  {metrics.fmt_bytes(detail['base']['bytes'])}")
    print(f"variant   {detail['variant']['id']}  {metrics.fmt_bytes(detail['variant']['bytes'])}")
    print(f"codec     {detail['codec']['compressor']} level {detail['codec']['level']}")
    print()
    print(f"{'op':<12}{'count':>7}{'raw':>14}{'stored':>14}")
    for op, bucket in sorted(detail["by_op"].items()):
        print(
            f"{op:<12}{bucket['count']:>7}{metrics.fmt_bytes(bucket['raw_bytes']):>14}"
            f"{metrics.fmt_bytes(bucket['stored_bytes']):>14}"
        )
    print()
    limit = args.top
    print(f"{'tensor':<44}{'op':<11}{'raw':>12}{'stored':>12}{'ratio':>8}")
    for tensor in detail["tensors"][:limit]:
        ratio = "0.0%" if tensor["ratio"] is None else f"{tensor['ratio']:.1%}"
        print(
            f"{str(tensor['name'])[:43]:<44}{tensor['op']:<11}"
            f"{metrics.fmt_bytes(tensor['raw_bytes']):>12}{metrics.fmt_bytes(tensor['stored_bytes']):>12}{ratio:>8}"
        )
    hidden = len(detail["tensors"]) - limit
    if hidden > 0:
        print(f"… {hidden} more tensors (--top to show more)")
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    """Compare CCP against the two baselines a buyer will ask about."""
    import gzip

    base = _read(args.base)
    variant = _read(args.variant)

    t0 = time.perf_counter()
    gz = len(gzip.compress(variant, 6))
    gz_seconds = time.perf_counter() - t0

    container, stats = packer.pack(base, variant, base_id="base", variant_id="variant", compressor=args.compressor)

    t0 = time.perf_counter()
    rebuilt = packer.unpack(container, base, verify=True)
    decode_seconds = time.perf_counter() - t0

    raw = len(variant)
    rows = [
        ("full copy (what hubs ship today)", raw, None),
        (f"gzip -6 ({gz_seconds:.1f}s)", gz, None),
        (f"CCP ({stats.encode_seconds:.1f}s encode, {decode_seconds:.1f}s rebuild)", len(container), None),
    ]
    print(f"{'representation':<50}{'bytes':>14}{'of full':>10}")
    for label, size, _ in rows:
        print(f"{label:<50}{metrics.fmt_bytes(size):>14}{size / raw:>9.1%}")
    print()
    print(f"CCP vs full copy   {1 - len(container) / raw:.1%} smaller")
    print(f"CCP vs gzip -6     {1 - len(container) / gz:.1%} smaller")
    print(f"lossless           {sha256(rebuilt) == sha256(variant)}")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """Generate the demo family and print the measured table."""
    from demo.generate_models import Arch, DEFAULT_VARIANTS, build_base, build_variant

    arch = Arch(d_model=args.d_model, n_layers=args.layers, vocab=args.vocab)
    sys.stderr.write(f"generating base · {arch.param_count():,} params\n")
    base_blob, tensors = build_base(arch)

    print(f"{'variant':<24}{'kind':<20}{'full':>12}{'ccp':>12}{'saving':>9}{'lossless':>10}")
    total_raw = total_ccp = 0
    for spec in DEFAULT_VARIANTS:
        blob = build_variant(tensors, spec, arch)
        container, stats = packer.pack(base_blob, blob, base_id="base", variant_id=spec.variant_id)
        rebuilt = packer.unpack(container, base_blob)
        total_raw += len(blob)
        total_ccp += len(container)
        print(
            f"{spec.variant_id:<24}{spec.kind:<20}"
            f"{metrics.fmt_bytes(len(blob)):>12}{metrics.fmt_bytes(len(container)):>12}"
            f"{stats.savings_ratio:>8.1%}{str(sha256(rebuilt) == sha256(blob)):>10}"
        )
    print(
        f"\n{'fleet':<44}{metrics.fmt_bytes(total_raw):>12}{metrics.fmt_bytes(total_ccp):>12}"
        f"{1 - total_ccp / total_raw:>8.1%}"
    )
    projection = metrics.project_at_scale(measured_variant_ratio=total_ccp / total_raw).as_dict()
    print(
        f"\nprojection · 7B model, 10 variants, 100k downloads each:\n"
        f"  egress  ${projection['egress']['baseline_usd']:,.0f} → ${projection['egress']['ccp_usd']:,.0f}"
        f"  (saves ${projection['egress']['saved_usd']:,.0f})\n"
        f"  storage ${projection['storage']['baseline_usd_month']:,.0f}/mo → "
        f"${projection['storage']['ccp_usd_month']:,.0f}/mo"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ccp", description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="command", required=True)

    p = subparsers.add_parser("pack", help="encode a variant against a base")
    p.add_argument("--base", required=True)
    p.add_argument("--variant", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--compressor", default="zlib", choices=["zlib", "lzma", "none"])
    p.set_defaults(func=cmd_pack)

    p = subparsers.add_parser("unpack", help="rebuild a variant from a container plus its base")
    p.add_argument("--base", required=True)
    p.add_argument("--container", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_unpack)

    p = subparsers.add_parser("inspect", help="print a container's per-tensor plan")
    p.add_argument("container")
    p.add_argument("--top", type=int, default=20)
    p.set_defaults(func=cmd_inspect)

    p = subparsers.add_parser("bench", help="compare CCP against a full copy and gzip")
    p.add_argument("--base", required=True)
    p.add_argument("--variant", required=True)
    p.add_argument("--compressor", default="zlib", choices=["zlib", "lzma", "none"])
    p.set_defaults(func=cmd_bench)

    p = subparsers.add_parser("demo", help="generate the demo family and measure it")
    p.add_argument("--d-model", dest="d_model", type=int, default=128)
    p.add_argument("--layers", type=int, default=4)
    p.add_argument("--vocab", type=int, default=8192)
    p.set_defaults(func=cmd_demo)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (packer.ContainerError, packer.CodecError, safetensors.SafetensorsError) as exc:
        # An engine-level refusal is the expected failure mode, not a crash to
        # dump a traceback for.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"error: {exc.filename}: no such file", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
