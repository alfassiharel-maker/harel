"""End-to-end tests for the CCP vertical slice.

Every test here asserts on real stored bytes. The invariant under test throughout
is the one the product cannot compromise on: what comes out is byte-for-byte what
went in.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from ccp.engine import Engine, IntegrityError

BLOCK = 64 * 1024


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def store_series(engine: Engine, repo: Path, artifacts: dict, names: list[str]) -> list[dict]:
    """Stores the named versions as a chain, each based on the previous."""
    results = []
    previous = None
    for name in names:
        results.append(
            engine.store(repo, artifacts[name], name, base=previous, block_size=BLOCK)
        )
        previous = name
    return results


def assert_roundtrip(engine: Engine, repo: Path, name: str, original: Path, tmp_path: Path) -> None:
    """The core assertion: reconstruct, then compare bytes and hashes."""
    out = tmp_path / f"{name}.out"
    result = engine.reconstruct(repo, name, out)

    assert out.read_bytes() == original.read_bytes(), f"{name} is not byte-for-byte identical"
    assert sha256_file(out) == sha256_file(original), f"{name} hash mismatch"
    # The engine's own recorded hash must agree with the file we just wrote — this
    # catches a container that verifies against a wrong-but-consistent digest.
    assert result["content_sha256"] == sha256_file(original)
    assert result["integrity_verified"] is True
    assert result["size"] == original.stat().st_size


class TestPipeline:
    def test_the_whole_pipeline_produces_identical_bytes(self, engine, repo, artifacts, tmp_path):
        """Real base and target through every stage: streaming, blocks, C++ XOR and
        popcount, the Julia decision, storage, reconstruction and SHA-256."""
        store_series(engine, repo, artifacts, ["v1", "v2", "v3", "v4"])
        for name in ["v1", "v2", "v3", "v4"]:
            assert_roundtrip(engine, repo, name, artifacts[name], tmp_path)

    def test_a_sparse_change_is_stored_as_a_sparse_delta(self, engine, repo, artifacts):
        """The case the product exists for: a small in-place change must cost a
        small fraction of the artifact."""
        results = store_series(engine, repo, artifacts, ["v1", "v2"])
        v1, v2 = results

        assert v1["base"] is None, "the first version must be a root"
        assert all(b["kind"] == "FULL" for b in v1["blocks"]), "a root can only hold FULL blocks"

        kinds = {b["kind"] for b in v2["blocks"]}
        assert kinds == {"DELTA_SPARSE"}, f"expected sparse deltas throughout, got {kinds}"
        assert v2["stored_size"] < v2["artifact_size"] // 100, (
            f"a change of {v2['changed_bytes']} bytes stored "
            f"{v2['stored_size']} bytes of {v2['artifact_size']}"
        )
        # The measurements must be real, not defaults.
        assert v2["changed_bytes"] == 64, "one byte changed per 8 KiB across 512 KiB"
        assert v2["changed_bits"] > 0

    def test_unrelated_data_is_stored_full_not_delta(self, engine, repo, artifacts):
        """The rule that makes CCP a product rather than a bet: when a delta would
        cost more, the system must refuse it."""
        results = store_series(engine, repo, artifacts, ["v1", "v4"])
        v4 = results[1]

        assert {b["kind"] for b in v4["blocks"]} == {"FULL"}
        assert "beats the cheapest delta" in v4["blocks"][0]["reason"]
        # A delta here would have cost ~5x the artifact; FULL must stay close to 1x.
        assert v4["stored_over_full"] < 1.01

    def test_an_unchanged_artifact_costs_almost_nothing(self, engine, repo, artifacts, tmp_path):
        """Storing the same bytes twice must reduce to references, and still
        reconstruct exactly."""
        engine.store(repo, artifacts["v1"], "first", block_size=BLOCK)
        same = tmp_path / "same.bin"
        same.write_bytes(artifacts["v1"].read_bytes())
        result = engine.store(repo, same, "second", base="first", block_size=BLOCK)

        assert {b["kind"] for b in result["blocks"]} == {"IDENTICAL"}
        assert result["changed_bytes"] == 0
        assert all(b["payload_len"] == 0 for b in result["blocks"])
        assert_roundtrip(engine, repo, "second", artifacts["v1"], tmp_path)


class TestChains:
    def test_a_chain_reconstructs_through_every_hop(self, engine, repo, artifacts, tmp_path):
        results = store_series(engine, repo, artifacts, ["v1", "v2", "v3"])
        assert [r["chain_depth"] for r in results] == [0, 1, 2]
        # The deepest version is the one that exercises the whole chain walk.
        assert_roundtrip(engine, repo, "v3", artifacts["v3"], tmp_path)

    def test_a_long_chain_stays_exact(self, engine, repo, tmp_path):
        """Ten hops, each a small in-place change. Chain depth is where a
        reconstruction bug would compound instead of showing up immediately."""
        from conftest import pseudo_random_bytes

        size = 128 * 1024
        current = bytearray(pseudo_random_bytes(size, seed=7))
        originals = {}
        previous = None

        for step in range(10):
            if step > 0:
                for offset in range(step, size, 4096):
                    current[offset] ^= (step * 37) % 256
            name = f"gen{step}"
            path = tmp_path / f"{name}.bin"
            path.write_bytes(bytes(current))
            originals[name] = path
            engine.store(repo, path, name, base=previous, block_size=BLOCK)
            previous = name

        for name, path in originals.items():
            assert_roundtrip(engine, repo, name, path, tmp_path)


class TestUnequalLengths:
    """XOR is undefined for unequal lengths, so the policy must be explicit and
    lossless — never silent padding or truncation."""

    def test_a_longer_target_reconstructs_exactly(self, engine, repo, artifacts, tmp_path):
        from conftest import pseudo_random_bytes

        base = artifacts["v1"].read_bytes()
        longer = tmp_path / "longer.bin"
        longer.write_bytes(base + pseudo_random_bytes(9999, seed=3))

        engine.store(repo, artifacts["v1"], "base", block_size=BLOCK)
        result = engine.store(repo, longer, "longer", base="base", block_size=BLOCK)

        assert result["artifact_size"] == len(base) + 9999
        assert_roundtrip(engine, repo, "longer", longer, tmp_path)

    def test_a_shorter_target_reconstructs_exactly(self, engine, repo, artifacts, tmp_path):
        base = artifacts["v1"].read_bytes()
        shorter = tmp_path / "shorter.bin"
        shorter.write_bytes(base[: len(base) - 12345])

        engine.store(repo, artifacts["v1"], "base", block_size=BLOCK)
        engine.store(repo, shorter, "shorter", base="base", block_size=BLOCK)
        assert_roundtrip(engine, repo, "shorter", shorter, tmp_path)

    def test_the_tail_block_of_an_unaligned_artifact(self, engine, repo, tmp_path):
        """Sizes that are deliberately not multiples of the block size: the tail
        block is where an off-by-one silently truncates data."""
        from conftest import pseudo_random_bytes

        for size in [1, 2, BLOCK - 1, BLOCK, BLOCK + 1, BLOCK * 2 + 7]:
            r = tmp_path / f"repo-{size}"
            a = tmp_path / f"a-{size}.bin"
            b = tmp_path / f"b-{size}.bin"
            data = bytearray(pseudo_random_bytes(size, seed=size))
            a.write_bytes(bytes(data))
            data[size // 2] ^= 0xFF
            b.write_bytes(bytes(data))

            engine.store(r, a, "a", block_size=BLOCK)
            engine.store(r, b, "b", base="a", block_size=BLOCK)
            assert_roundtrip(engine, r, "a", a, tmp_path)
            assert_roundtrip(engine, r, "b", b, tmp_path)


class TestIntegrity:
    def test_a_corrupted_payload_is_detected(self, engine, repo, artifacts, tmp_path):
        """A single flipped bit anywhere in a container must be caught, and the
        failure must be an integrity error rather than silently wrong output."""
        store_series(engine, repo, artifacts, ["v1", "v2"])

        containers = sorted((repo / "objects").glob("*.ccp"))
        assert containers, "storing must produce containers"
        target = max(containers, key=lambda p: p.stat().st_size)

        data = bytearray(target.read_bytes())
        # Middle of the payload region: past the header and index, before the footer.
        data[len(data) // 2] ^= 0x01
        target.write_bytes(bytes(data))

        with pytest.raises(IntegrityError):
            engine.verify(repo)

    def test_a_truncated_container_is_detected(self, engine, repo, artifacts):
        engine.store(repo, artifacts["v1"], "v1", block_size=BLOCK)
        container = next((repo / "objects").glob("*.ccp"))
        data = container.read_bytes()
        container.write_bytes(data[: len(data) - 64])

        with pytest.raises(IntegrityError):
            engine.verify(repo)

    def test_verify_passes_on_intact_data(self, engine, repo, artifacts):
        store_series(engine, repo, artifacts, ["v1", "v2", "v3"])
        result = engine.verify(repo)
        assert result["verified"] == 3
        assert all(r["verified"] for r in result["results"])


class TestRepository:
    def test_inspect_reports_the_stored_representations(self, engine, repo, artifacts):
        store_series(engine, repo, artifacts, ["v1", "v2"])
        info = engine.inspect(repo, "v2")

        assert info["format_version"] == 1
        assert info["is_root"] is False
        assert info["container_integrity_verified"] is True
        assert info["block_count"] == len(info["blocks"])
        assert sum(k["blocks"] for k in info["representation_summary"]) == info["block_count"]
        # The measurements behind each decision must be retained in the container.
        assert any(b["changed_bytes"] > 0 for b in info["blocks"])

    def test_list_totals_match_the_stored_versions(self, engine, repo, artifacts):
        results = store_series(engine, repo, artifacts, ["v1", "v2", "v3"])
        listing = engine.list_versions(repo)

        assert listing["count"] == 3
        assert listing["total_stored_size"] == sum(r["stored_size"] for r in results)
        assert listing["total_logical_size"] == sum(r["artifact_size"] for r in results)

    def test_a_duplicate_name_is_refused(self, engine, repo, artifacts):
        engine.store(repo, artifacts["v1"], "v1", block_size=BLOCK)
        with pytest.raises(Exception, match="already exists"):
            engine.store(repo, artifacts["v2"], "v1", block_size=BLOCK)

    def test_an_unknown_base_is_refused(self, engine, repo, artifacts):
        with pytest.raises(Exception, match="no version"):
            engine.store(repo, artifacts["v1"], "v1", base="nonexistent", block_size=BLOCK)

    def test_a_repository_survives_being_reopened(self, engine, repo, artifacts, tmp_path):
        """The manifest is the version graph; it must round-trip through disk."""
        store_series(engine, repo, artifacts, ["v1", "v2"])
        assert (repo / "manifest.json").exists()

        reopened = Engine(engine.config)
        assert reopened.list_versions(repo)["count"] == 2
        assert_roundtrip(reopened, repo, "v2", artifacts["v2"], tmp_path)


class TestEngineWiring:
    def test_the_cpp_engine_is_linked_and_dispatching(self, engine):
        """Confirms the bit engine is real: a stubbed build would not report a
        version string or a scalar execution path."""
        info = engine.info()
        assert info["bitexec"].startswith("ccp-bitexec")
        assert "scalar" in info["bitexec_features"]
        assert info["container_format_version"] == 1

    def test_the_julia_engine_supplies_the_reasoning(self, engine, repo, artifacts):
        """Every decision must carry the strategy engine's own explanation. An
        empty reason would mean the decision came from somewhere else."""
        store_series(engine, repo, artifacts, ["v1", "v2"])
        result = engine.inspect(repo, "v2")
        assert result["block_count"] > 0

        stored = engine.store(repo, artifacts["v3"], "v3", base="v2", block_size=BLOCK)
        assert all(b["reason"] for b in stored["blocks"])
        assert any("cost" in b["reason"] for b in stored["blocks"])
