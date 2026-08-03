"""Tests for the CCP experiment engine.

Runs with no dependencies installed:

    cd experiments/ccp && python3 -m unittest discover -s tests -t .
"""

from __future__ import annotations

import hashlib
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ccp_datasets  # noqa: E402
import ccp_full_experiment as ccp  # noqa: E402
from ccp_layer_analysis import deinterleave, reinterleave  # noqa: E402


def repeated_with_mutations(
    region_size: int, regions: int, changes_per_region: int, seed: int = 7
) -> bytes:
    """A base region repeated with a fixed number of mutated bytes."""
    base = ccp_datasets.hash_bytes(region_size, seed)
    out = bytearray(base)
    for r in range(1, regions):
        mutated = bytearray(base)
        for j in range(changes_per_region):
            mutated[(j * 131 + r * 17) % region_size] ^= (j + r) & 0xFF or 1
        out += mutated
    return bytes(out)


class TestPositionWidth(unittest.TestCase):
    def test_addresses_last_byte_of_region(self) -> None:
        for region_size in (256, 4096, 65536, 1 << 20, 1 << 22):
            width = ccp.position_width(region_size)
            self.assertGreaterEqual(
                256**width,
                region_size,
                f"{width} bytes cannot address {region_size} positions",
            )

    def test_known_widths(self) -> None:
        self.assertEqual(ccp.position_width(256), 1)
        self.assertEqual(ccp.position_width(257), 2)
        self.assertEqual(ccp.position_width(65536), 2)
        self.assertEqual(ccp.position_width(65537), 3)


class TestCostModel(unittest.TestCase):
    def test_zero_changes_costs_only_the_count_header(self) -> None:
        self.assertEqual(
            ccp.model_delta_bits(0, 4096), ccp.COUNT_STRUCT.size * 8
        )

    def test_hand_computed_delta_cost(self) -> None:
        # A 64KB region needs 2 bytes to address a position, plus 1 byte for the
        # changed value: 3 bytes per change. Ten changes is 30 bytes, and the
        # record carries a 4-byte count header, so 34 bytes = 272 bits.
        self.assertEqual(ccp.model_delta_bits(10, 65536), 272)

    def test_break_even_matches_the_pay_off_test(self) -> None:
        for region_size in (4096, 65536, 1 << 20):
            k = ccp.break_even_changes(region_size)
            self.assertTrue(
                ccp.ccp_pays_off(k, region_size),
                f"k={k} should still pay off at region {region_size}",
            )
            self.assertFalse(
                ccp.ccp_pays_off(k + 1, region_size),
                f"k={k + 1} should not pay off at region {region_size}",
            )

    def test_hand_computed_break_even(self) -> None:
        # 4KB region: 4096*8 = 32768 bits of verbatim storage. A delta entry is
        # 2 bytes of position plus 1 byte of value = 24 bits; the count header
        # takes 32 bits. (32768 - 32 - 1) // 24 = 1363.
        self.assertEqual(ccp.break_even_changes(4096), 1363)

    def test_rejects_negative_change_count(self) -> None:
        with self.assertRaises(ValueError):
            ccp.model_delta_bits(-1, 4096)


class TestDeltaPrimitives(unittest.TestCase):
    def test_anchor_identical_regions_have_no_delta(self) -> None:
        a = b"\x11\x22\x33\x44"
        self.assertEqual(ccp.delta_entries(memoryview(a), memoryview(a)), [])
        self.assertEqual(ccp.diff_count(memoryview(a), memoryview(a)), 0)

    def test_hand_computed_delta(self) -> None:
        base = bytes([0x00, 0xFF, 0x0F, 0x10])
        target = bytes([0x00, 0xF0, 0x0F, 0x11])
        # Positions 1 and 3 differ: 0xFF^0xF0 = 0x0F, 0x10^0x11 = 0x01.
        self.assertEqual(
            ccp.delta_entries(memoryview(base), memoryview(target)),
            [(1, 0x0F), (3, 0x01)],
        )
        self.assertEqual(ccp.diff_count(memoryview(base), memoryview(target)), 2)

    def test_delta_applies_back_to_the_target(self) -> None:
        base = ccp_datasets.hash_bytes(1024, 3)
        target = bytearray(base)
        for pos in (0, 5, 511, 1023):
            target[pos] ^= 0xA5
        delta = ccp.delta_entries(memoryview(base), memoryview(bytes(target)))
        self.assertEqual(ccp.apply_delta(memoryview(base), delta), bytes(target))

    def test_every_position_differing_is_reported(self) -> None:
        base = bytes(64)
        target = bytes(range(1, 65))
        delta = ccp.delta_entries(memoryview(base), memoryview(target))
        self.assertEqual(len(delta), 64)
        self.assertEqual(ccp.apply_delta(memoryview(base), delta), target)

    def test_length_mismatch_is_an_error(self) -> None:
        with self.assertRaises(ValueError):
            ccp.xor_bytes(memoryview(b"abc"), memoryview(b"ab"))


class TestRoundTrip(unittest.TestCase):
    def assert_round_trip(self, data: bytes, region_size: int) -> bytes:
        container = ccp.encode_bytes(data, region_size)
        self.assertEqual(ccp.decode_bytes(container), data)
        self.assertEqual(
            hashlib.sha256(ccp.decode_bytes(container)).hexdigest(),
            hashlib.sha256(data).hexdigest(),
        )
        return container

    def test_near_duplicate_regions_round_trip(self) -> None:
        data = repeated_with_mutations(4096, 32, 20)
        self.assert_round_trip(data, 4096)

    def test_incompressible_data_round_trips(self) -> None:
        data = ccp_datasets.hash_bytes(64 * 1024, 11)
        self.assert_round_trip(data, 4096)

    def test_all_zero_data_round_trips(self) -> None:
        self.assert_round_trip(bytes(32 * 1024), 4096)

    def test_partial_trailing_region_round_trips(self) -> None:
        data = repeated_with_mutations(1024, 8, 5) + b"tail-bytes-not-a-full-region"
        self.assert_round_trip(data, 1024)

    def test_data_shorter_than_one_region_round_trips(self) -> None:
        self.assert_round_trip(b"only a few bytes", 4096)

    def test_single_exact_region_round_trips(self) -> None:
        data = ccp_datasets.hash_bytes(4096, 5)
        self.assert_round_trip(data, 4096)

    def test_exactly_repeated_regions_round_trip(self) -> None:
        block = ccp_datasets.hash_bytes(2048, 13)
        self.assert_round_trip(block * 16, 2048)

    def test_every_synthetic_dataset_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for name in sorted(ccp_datasets.GENERATORS):
                path = os.path.join(tmp, f"{name}.bin")
                ccp_datasets.build(name, path, 256 * 1024, 1)
                with open(path, "rb") as f:
                    data = f.read()
                with self.subTest(dataset=name):
                    self.assert_round_trip(data, 4096)


class TestDecisionRule(unittest.TestCase):
    def test_near_duplicates_are_stored_as_deltas(self) -> None:
        data = repeated_with_mutations(4096, 32, 20)
        container = ccp.encode_bytes(data, 4096)
        self.assertLess(
            len(container),
            len(data) // 2,
            "regions differing in 20 bytes should encode far below verbatim",
        )

    def test_unrelated_regions_are_never_forced_into_deltas(self) -> None:
        data = ccp_datasets.hash_bytes(64 * 1024, 21)
        container = ccp.encode_bytes(data, 4096)
        # Overhead is bounded by the header plus one index entry per region.
        overhead = ccp.HEADER_STRUCT.size + ccp.TABLE_STRUCT.size * (len(data) // 4096)
        self.assertLessEqual(len(container), len(data) + overhead)

    def test_a_file_can_contain_both_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "drift.bin")
            ccp_datasets.gen_drifting_duplicate(path, 4 * 1024 * 1024, 1)
            with open(path, "rb") as f:
                data = f.read()
        container = ccp.encode_bytes(data, 64 * 1024)
        view = memoryview(container)
        try:
            header = ccp.read_header(view)
            kinds = [
                ccp.TABLE_STRUCT.unpack_from(
                    view, ccp.HEADER_STRUCT.size + i * ccp.TABLE_STRUCT.size
                )[0]
                for i in range(header.region_count)
            ]
        finally:
            view.release()
        self.assertIn(ccp.TYPE_CCP, kinds, "no region was stored as a delta")
        self.assertIn(ccp.TYPE_ORIGINAL, kinds, "no region was stored verbatim")
        self.assertEqual(ccp.decode_bytes(container), data)

    def test_a_base_is_never_itself_a_delta(self) -> None:
        data = repeated_with_mutations(4096, 24, 15)
        container = ccp.encode_bytes(data, 4096)
        view = memoryview(container)
        try:
            header = ccp.read_header(view)
            table = [
                ccp.TABLE_STRUCT.unpack_from(
                    view, ccp.HEADER_STRUCT.size + i * ccp.TABLE_STRUCT.size
                )
                for i in range(header.region_count)
            ]
        finally:
            view.release()
        for index, (kind, base) in enumerate(table):
            if kind == ccp.TYPE_CCP:
                self.assertEqual(
                    table[base][0],
                    ccp.TYPE_ORIGINAL,
                    f"region {index} references a base that is itself a delta",
                )


class TestContainerValidation(unittest.TestCase):
    def setUp(self) -> None:
        self.data = repeated_with_mutations(1024, 8, 6)
        self.container = ccp.encode_bytes(self.data, 1024)

    def test_bad_magic_is_rejected(self) -> None:
        corrupt = b"XXXX" + self.container[4:]
        with self.assertRaises(ValueError):
            ccp.decode_bytes(corrupt)

    def test_truncated_container_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ccp.decode_bytes(self.container[: len(self.container) // 2])

    def test_empty_input_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ccp.decode_bytes(b"")

    def test_unknown_version_is_rejected(self) -> None:
        corrupt = bytearray(self.container)
        corrupt[4] = 99
        with self.assertRaises(ValueError):
            ccp.decode_bytes(bytes(corrupt))

    def test_inconsistent_position_width_is_rejected(self) -> None:
        corrupt = bytearray(self.container)
        offset = ccp.HEADER_STRUCT.size - 32 - 1
        self.assertEqual(corrupt[offset], ccp.position_width(1024))
        corrupt[offset] = 7
        with self.assertRaises(ValueError):
            ccp.decode_bytes(bytes(corrupt))

    def test_out_of_range_base_index_is_rejected(self) -> None:
        corrupt = bytearray(self.container)
        for index in range(8):
            entry = ccp.HEADER_STRUCT.size + index * ccp.TABLE_STRUCT.size
            if corrupt[entry] == ccp.TYPE_CCP:
                corrupt[entry + 1 : entry + 5] = (9999).to_bytes(4, "little")
                with self.assertRaises(ValueError):
                    ccp.decode_bytes(bytes(corrupt))
                return
        self.skipTest("fixture contained no delta region to corrupt")

    def test_out_of_range_delta_position_is_rejected(self) -> None:
        view = memoryview(bytes(self.container))
        header = ccp.read_header(view)
        view.release()
        offset = ccp.HEADER_STRUCT.size + header.region_count * ccp.TABLE_STRUCT.size
        for index in range(header.region_count):
            entry = ccp.HEADER_STRUCT.size + index * ccp.TABLE_STRUCT.size
            kind = self.container[entry]
            if kind == ccp.TYPE_ORIGINAL:
                offset += header.region_size
                continue
            count = int.from_bytes(
                self.container[offset : offset + ccp.COUNT_STRUCT.size], "little"
            )
            self.assertGreater(count, 0)
            corrupt = bytearray(self.container)
            position_at = offset + ccp.COUNT_STRUCT.size
            corrupt[position_at : position_at + header.pos_width] = (
                header.region_size
            ).to_bytes(header.pos_width, "little")
            with self.assertRaises(ValueError):
                ccp.decode_bytes(bytes(corrupt))
            return
        self.skipTest("fixture contained no delta region to corrupt")


class TestClustering(unittest.TestCase):
    def test_identical_regions_land_together(self) -> None:
        block = ccp_datasets.hash_bytes(4096, 2)
        data = block * 6
        view = memoryview(data)
        regions = ccp.RegionView(view, 4096)
        signatures = [ccp.region_signature(regions[i]) for i in range(len(regions))]
        clusters = ccp.cluster_regions(signatures)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(sorted(clusters[0]), list(range(6)))

    def test_unrelated_regions_stay_apart(self) -> None:
        data = ccp_datasets.hash_bytes(4096 * 6, 4)
        view = memoryview(data)
        regions = ccp.RegionView(view, 4096)
        signatures = [ccp.region_signature(regions[i]) for i in range(len(regions))]
        clusters = ccp.cluster_regions(signatures)
        self.assertEqual(len(clusters), 6)

    def test_every_region_is_assigned_exactly_once(self) -> None:
        data = repeated_with_mutations(4096, 20, 30) + ccp_datasets.hash_bytes(
            4096 * 5, 9
        )
        view = memoryview(data)
        regions = ccp.RegionView(view, 4096)
        signatures = [ccp.region_signature(regions[i]) for i in range(len(regions))]
        clusters = ccp.cluster_regions(signatures)
        assigned = [i for cluster in clusters for i in cluster]
        self.assertEqual(sorted(assigned), list(range(len(regions))))

    def test_base_selection_prefers_a_central_region(self) -> None:
        base = ccp_datasets.hash_bytes(1024, 6)
        far = bytes(b ^ 0xFF for b in base)
        data = base + base + far
        view = memoryview(data)
        regions = ccp.RegionView(view, 1024)
        chosen = ccp.select_base(regions, [0, 1, 2])
        self.assertIn(chosen, (0, 1), "the outlier region should not become the base")


class TestRegionView(unittest.TestCase):
    def test_tail_is_separated_from_full_regions(self) -> None:
        view = memoryview(bytes(2048 + 17))
        regions = ccp.RegionView(view, 1024)
        self.assertEqual(len(regions), 2)
        self.assertEqual(regions.tail_length, 17)

    def test_out_of_range_index_raises(self) -> None:
        regions = ccp.RegionView(memoryview(bytes(2048)), 1024)
        with self.assertRaises(IndexError):
            regions[2]


class TestUndefinedCases(unittest.TestCase):
    def test_zero_region_size_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ccp.encode(memoryview(b"abcd"), 0, hashlib.sha256(b"abcd").digest(), io.BytesIO())

    def test_empty_input_produces_no_regions(self) -> None:
        buffer = io.BytesIO()
        stats = ccp.encode(memoryview(b""), 4096, hashlib.sha256(b"").digest(), buffer)
        self.assertEqual(stats.regions_total, 0)
        self.assertEqual(ccp.decode_bytes(buffer.getvalue()), b"")

    def test_parse_size_rejects_zero(self) -> None:
        with self.assertRaises(ValueError):
            ccp.parse_size("0MB")

    def test_unknown_dataset_name_raises(self) -> None:
        with self.assertRaises(KeyError):
            ccp_datasets.build("no_such_dataset", "/dev/null", 1024)


class TestSizeParsing(unittest.TestCase):
    def test_known_values(self) -> None:
        self.assertEqual(ccp.parse_size("4KB"), 4096)
        self.assertEqual(ccp.parse_size("1MB"), 1024 * 1024)
        self.assertEqual(ccp.parse_size("2GB"), 2 * 1024**3)
        self.assertEqual(ccp.parse_size("512"), 512)
        self.assertEqual(ccp.parse_size("512B"), 512)


class TestByteePlaneTransform(unittest.TestCase):
    def test_reordering_is_reversible(self) -> None:
        for width in (2, 4, 8):
            for length in (0, 1, width - 1, width, width * 5, width * 5 + 3):
                data = ccp_datasets.hash_bytes(length, width) if length else b""
                with self.subTest(width=width, length=length):
                    self.assertEqual(
                        reinterleave(deinterleave(data, width), width, len(data)),
                        data,
                    )

    def test_width_one_is_the_identity(self) -> None:
        data = ccp_datasets.hash_bytes(100, 1)
        self.assertEqual(deinterleave(data, 1), data)


class TestEndToEnd(unittest.TestCase):
    def test_experiment_reports_a_pass_and_a_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "input.bin")
            with open(source, "wb") as f:
                f.write(repeated_with_mutations(4096, 64, 20))
            experiment = ccp.run_experiment(
                source, "test input", [4096], tmp, verbose=False
            )
        self.assertEqual(len(experiment.results), 1)
        self.assertEqual(experiment.results[0].reconstruction, "PASS")
        self.assertIn("SAVING", experiment.verdict)
        self.assertIn("CCP FULL EXPERIMENT REPORT", ccp.render_report(experiment))

    def test_experiment_on_incompressible_data_reports_no_saving(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "random.bin")
            ccp.write_random_file(source, 2 * 1024 * 1024, seed=1)
            experiment = ccp.run_experiment(
                source, "control", [4096, 65536], tmp, verbose=False
            )
        for result in experiment.results:
            self.assertEqual(result.reconstruction, "PASS")
            self.assertLessEqual(result.saving_percent, 0.0)
        self.assertIn("NO SAVING", experiment.verdict)

    def test_measured_container_matches_the_analytic_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "input.bin")
            with open(source, "wb") as f:
                f.write(repeated_with_mutations(4096, 48, 25))
            experiment = ccp.run_experiment(
                source, "test input", [4096], tmp, verbose=False
            )
        result = experiment.results[0]
        self.assertLessEqual(
            abs(result.model_bytes - result.ccp_bytes),
            max(8, result.regions_total),
            "the analytic cost model and the encoder have drifted apart",
        )

    def test_determinism(self) -> None:
        data = repeated_with_mutations(4096, 16, 12)
        self.assertEqual(ccp.encode_bytes(data, 4096), ccp.encode_bytes(data, 4096))

    def test_datasets_are_reproducible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            digests = []
            for run in range(2):
                path = os.path.join(tmp, f"run{run}.bin")
                ccp_datasets.build("fp32_weights", path, 128 * 1024, 1)
                with open(path, "rb") as f:
                    digests.append(hashlib.sha256(f.read()).hexdigest())
        self.assertEqual(digests[0], digests[1])


if __name__ == "__main__":
    unittest.main()
