"""Tests for the benchmark harness.

The harness makes quantitative claims, so what is tested is that its numbers are
real: measured from the files on disk, and compared against baselines that are
actually computed rather than assumed.
"""

from __future__ import annotations

import zlib

from ccp import benchmark


def test_baselines_are_measured_not_assumed(tmp_path):
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    a.write_bytes(b"x" * 100_000)
    b.write_bytes(b"y" * 100_000)

    independent = benchmark.compressed_size([a, b], together=False)
    expected = len(zlib.compress(a.read_bytes(), 6)) + len(zlib.compress(b.read_bytes(), 6))
    assert independent == expected, "independent compression must equal per-file zlib"

    concatenated = benchmark.compressed_size([a, b], together=True)
    # Compressed together, cross-file redundancy is available, so the result must
    # not be larger than compressing separately.
    assert concatenated <= independent


def test_benchmark_reports_every_baseline_and_the_sparsity_measure(engine, repo, artifacts, tmp_path):
    result = benchmark.run(
        engine,
        repo,
        [artifacts["v1"], artifacts["v2"], artifacts["v3"]],
        ["v1", "v2", "v3"],
        block_size=64 * 1024,
        workdir=tmp_path / "work",
    )

    totals = result["totals"]
    for key in [
        "baseline_full_copies_bytes",
        "baseline_independent_zlib_bytes",
        "baseline_concatenated_zlib_bytes",
        "ccp_stored_bytes",
    ]:
        assert totals[key] > 0, f"{key} must be measured"

    # Encode and decode are both timed: an encode-only benchmark would flatter a
    # scheme whose read path is the expensive half.
    assert totals["decode_seconds"] > 0
    assert totals["encode_seconds"] > 0

    comparisons = result["comparisons"]
    assert comparisons["ccp_over_full_copies"] < 1.0, "sparse deltas must beat full copies here"
    # No assertion that CCP beats zlib: whether it does is a property of the
    # workload, and the harness exists to find out rather than to confirm.
    assert comparisons["ccp_over_independent_zlib"] is not None

    for v in result["versions"]:
        assert 0.0 <= v["changed_bit_fraction"] <= 1.0
        assert v["representations"], "every version must report what it stored"
