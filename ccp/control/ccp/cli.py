"""The CCP command line.

Thin by design: parse arguments, call the engine, present the result. No storage
logic and no policy live here.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import benchmark
from .config import Config
from .engine import Engine, EngineError, IntegrityError


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ccp",
        description="CCP — differential binary versioning and storage",
    )
    p.add_argument("--json", action="store_true", help="print raw JSON instead of a summary")
    p.add_argument("--block-size", type=int, default=None, help="block size in bytes")
    sub = p.add_subparsers(dest="command", required=True)

    store = sub.add_parser("store", help="ingest an artifact as a new version")
    store.add_argument("--repo", type=Path, required=True)
    store.add_argument("--file", type=Path, required=True)
    store.add_argument("--name", required=True)
    store.add_argument("--base", default=None, help="version to delta against")

    rec = sub.add_parser("reconstruct", help="materialise a version, verifying integrity")
    rec.add_argument("--repo", type=Path, required=True)
    rec.add_argument("--version", required=True)
    rec.add_argument("--out", type=Path, required=True)

    ls = sub.add_parser("list", help="list versions in a repository")
    ls.add_argument("--repo", type=Path, required=True)

    ins = sub.add_parser("inspect", help="show a container's blocks and representations")
    ins.add_argument("--repo", type=Path, required=True)
    ins.add_argument("--version", required=True)

    ver = sub.add_parser("verify", help="reconstruct and hash-check stored versions")
    ver.add_argument("--repo", type=Path, required=True)
    ver.add_argument("--version", default=None)

    bench = sub.add_parser("benchmark", help="measure a version series against real baselines")
    bench.add_argument("--repo", type=Path, required=True)
    bench.add_argument("--files", type=Path, nargs="+", required=True)
    bench.add_argument("--names", nargs="+", default=None, help="defaults to the file stems")

    sub.add_parser("info", help="report engine versions and capabilities")
    sub.add_parser("doctor", help="check that every component is present and runnable")

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = Config.discover(block_size=args.block_size)
    engine = Engine(config)

    try:
        if args.command == "doctor":
            return _doctor(config, engine, args.json)

        result = _dispatch(args, engine)
    except IntegrityError as e:
        # Loud and distinct: this means stored data is wrong, which is not the
        # same class of event as a mistyped path.
        print(f"INTEGRITY FAILURE: {e}", file=sys.stderr)
        return 3
    except EngineError as e:
        print(f"error ({e.kind}): {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        _summarise(args.command, result)
    return 0


def _dispatch(args: argparse.Namespace, engine: Engine) -> dict:
    if args.command == "store":
        return engine.store(args.repo, args.file, args.name, args.base, args.block_size)
    if args.command == "reconstruct":
        return engine.reconstruct(args.repo, args.version, args.out)
    if args.command == "list":
        return engine.list_versions(args.repo)
    if args.command == "inspect":
        return engine.inspect(args.repo, args.version)
    if args.command == "verify":
        return engine.verify(args.repo, args.version)
    if args.command == "info":
        return engine.info()
    if args.command == "benchmark":
        names = args.names or [f.stem for f in args.files]
        if len(names) != len(args.files):
            raise EngineError("--names must have one entry per file", kind="invalid")
        return benchmark.run(engine, args.repo, args.files, names, args.block_size)
    raise EngineError(f"unhandled command {args.command}", kind="invalid")


def _doctor(config: Config, engine: Engine, as_json: bool) -> int:
    missing = config.missing_components()
    report = {
        "engine_path": str(config.engine_path),
        "strategy_script": str(config.strategy_script),
        "julia": config.julia,
        "missing": missing,
    }
    if not missing:
        # Only meaningful proof that the whole stack runs: ask the engine, which
        # requires the binary to load the linked C++ library.
        report["engine"] = engine.info()
    report["ok"] = not missing

    if as_json:
        print(json.dumps(report, indent=2))
    elif missing:
        print("CCP is not ready:")
        for m in missing:
            print(f"  - {m}")
    else:
        e = report["engine"]
        print("CCP is ready.")
        print(f"  data engine    {e['dataeng_version']} ({config.engine_path})")
        print(f"  bit engine     {e['bitexec']} [{', '.join(e['bitexec_features'])}]")
        print(f"  strategy       {config.julia} {config.strategy_script}")
        print(f"  container fmt  v{e['container_format_version']}")
    return 0 if not missing else 1


def _mib(n: int) -> str:
    return f"{n / (1024 * 1024):.2f} MiB"


def _summarise(command: str, r: dict) -> None:
    if command == "store":
        kinds: dict[str, int] = {}
        for b in r["blocks"]:
            kinds[b["kind"]] = kinds.get(b["kind"], 0) + 1
        print(f"stored {r['name']}  (base: {r['base'] or 'none — root version'})")
        print(f"  artifact     {_mib(r['artifact_size'])}")
        print(f"  stored       {_mib(r['stored_size'])}  ({r['stored_over_full']:.4%} of full)")
        print(f"  changed      {r['changed_bytes']} bytes / {r['changed_bits']} bits")
        print(f"  chain depth  {r['chain_depth']}")
        print(f"  blocks       {r['block_count']} -> {kinds}")
        print(f"  sha256       {r['content_sha256']}")
        if r["blocks"]:
            print(f"  decision     {r['blocks'][0]['reason']}")
    elif command == "reconstruct":
        print(f"reconstructed {r['name']} -> {r['out']}")
        print(f"  {_mib(r['size'])}, chain depth {r['chain_depth']}")
        print(f"  sha256 {r['content_sha256']} (verified)")
    elif command == "list":
        print(f"{r['count']} versions")
        for v in r["versions"]:
            base = v["base_id"][:8] if v["base_id"] else "root"
            print(
                f"  {v['name']:<16} {_mib(v['size']):>12} logical  "
                f"{_mib(v['stored_size']):>12} stored  depth {v['chain_depth']}  base {base}"
            )
        if r["total_logical_size"]:
            pct = 100 * r["total_stored_size"] / r["total_logical_size"]
            print(f"  total: {_mib(r['total_stored_size'])} stored for "
                  f"{_mib(r['total_logical_size'])} logical ({pct:.2f}%)")
    elif command == "inspect":
        print(f"{r['name']}  container {r['container']}")
        print(f"  format v{r['format_version']}, {'root' if r['is_root'] else 'delta'}, "
              f"{r['block_count']} blocks of {r['block_size']} bytes")
        for k in r["representation_summary"]:
            print(f"  {k['kind']:<14} {k['blocks']} blocks")
    elif command == "verify":
        print(f"verified {r['verified']} versions, all reconstructed and hash-checked")
    elif command == "info":
        print(f"data engine {r['dataeng_version']}, {r['bitexec']} "
              f"[{', '.join(r['bitexec_features'])}], container format v{r['container_format_version']}")
    elif command == "benchmark":
        t = r["totals"]
        c = r["comparisons"]
        print("per version:")
        for v in r["versions"]:
            print(f"  {v['name']:<14} stored {_mib(v['stored_size']):>12}  "
                  f"changed bits {v['changed_bit_fraction']:.6%}  depth {v['chain_depth']}  "
                  f"{v['representations']}")
        print("\ntotals:")
        print(f"  logical                     {_mib(t['logical_bytes'])}")
        print(f"  CCP stored                  {_mib(t['ccp_stored_bytes'])}")
        print(f"  baseline: full copies       {_mib(t['baseline_full_copies_bytes'])}")
        print(f"  baseline: zlib per version  {_mib(t['baseline_independent_zlib_bytes'])}")
        print(f"  baseline: zlib concatenated {_mib(t['baseline_concatenated_zlib_bytes'])}")
        print(f"  encode {t['encode_seconds']}s ({t['encode_throughput_mib_s']} MiB/s), "
              f"decode {t['decode_seconds']}s ({t['decode_throughput_mib_s']} MiB/s)")
        print("\nCCP size relative to each baseline (below 1.0 means CCP stored less):")
        print(f"  vs full copies        {c['ccp_over_full_copies']}")
        print(f"  vs zlib per version   {c['ccp_over_independent_zlib']}")
        print(f"  vs zlib concatenated  {c['ccp_over_concatenated_zlib']}")
    else:
        print(json.dumps(r, indent=2))


if __name__ == "__main__":
    sys.exit(main())
