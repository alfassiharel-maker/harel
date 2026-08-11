"""Tests for the CCP Runtime and the public API.

    python3 -m unittest discover -s ccp/tests -t .

The central assertion, repeated in several shapes: a selective read returns
exactly what a full materialisation would have returned for that window. Nothing
here asserts on elapsed time -- timing is reported by the runtime but proves
nothing, and a timing assertion would be flaky.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest

from ccp.api import (
    CONTRACT,
    CCPFormatError,
    CCPIntegrityError,
    UnknownUnitError,
    build,
    describe_contract,
    open_representation,
)
from ccp.runtime import CCPRuntime


def deterministic_bytes(size: int, seed: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < size:
        out += hashlib.blake2b(
            counter.to_bytes(8, "little") + seed.to_bytes(8, "little"), digest_size=64
        ).digest()
        counter += 1
    return bytes(out[:size])


def near_duplicate_units(count: int, size: int, seed: int) -> dict:
    """Units sharing a body, each differing in one small region."""
    body = deterministic_bytes(size, seed)
    units = {"unit-00": body}
    for index in range(1, count):
        patch = deterministic_bytes(256, seed + index)
        cut = (index * 977) % (size - 256)
        units["unit-%02d" % index] = body[:cut] + patch + body[cut + 256 :]
    return units


class RuntimeFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.units = near_duplicate_units(8, 120_000, seed=51)
        self.container = build.from_units(self.units)
        self.rt = open_representation(self.container)


class TestLoading(RuntimeFixture):
    def test_loads_every_unit(self) -> None:
        self.assertEqual(sorted(self.rt.units()), sorted(self.units))
        self.assertEqual(len(self.rt), len(self.units))
        self.assertIn("unit-00", self.rt)
        self.assertNotIn("missing", self.rt)

    def test_load_from_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "model.ccp")
            with open(path, "wb") as handle:
                handle.write(self.container)
            runtime = open_representation(path, verify=True)
            self.assertTrue(runtime.is_verified)
            self.assertEqual(sorted(runtime.units()), sorted(self.units))

    def test_bad_container_is_rejected(self) -> None:
        with self.assertRaises(CCPFormatError):
            open_representation(b"not a ccp container at all")

    def test_truncated_container_is_rejected(self) -> None:
        with self.assertRaises(CCPFormatError):
            open_representation(self.container[: len(self.container) // 2])

    def test_container_over_the_limit_is_refused(self) -> None:
        with self.assertRaises(CCPFormatError):
            CCPRuntime.load(self.container, max_container_bytes=16)

    def test_non_bytes_is_rejected(self) -> None:
        with self.assertRaises(TypeError):
            CCPRuntime.load(12345)  # type: ignore[arg-type]

    def test_verify_on_load_detects_corruption(self) -> None:
        # Flip a byte in the container's payload region and confirm the runtime
        # refuses to hand it over rather than serving wrong bytes.
        corrupted = bytearray(self.container)
        corrupted[len(corrupted) - 64] ^= 0xFF
        with self.assertRaises((CCPIntegrityError, CCPFormatError)):
            open_representation(bytes(corrupted), verify=True)


class TestVerification(RuntimeFixture):
    def test_verify_passes_on_a_good_representation(self) -> None:
        report = self.rt.verify()
        self.assertTrue(report.ok)
        self.assertEqual(report.units_checked, len(self.units))
        self.assertEqual(report.units_ok, len(self.units))
        self.assertEqual(report.failures, ())
        self.assertEqual(
            report.bytes_verified, sum(len(v) for v in self.units.values())
        )
        self.assertTrue(self.rt.is_verified)

    def test_not_verified_until_asked(self) -> None:
        self.assertFalse(self.rt.is_verified)


class TestMaterialize(RuntimeFixture):
    def test_every_unit_round_trips(self) -> None:
        for uid, data in self.units.items():
            self.assertEqual(self.rt.materialize(uid), data)

    def test_unknown_unit_raises(self) -> None:
        with self.assertRaises(UnknownUnitError):
            self.rt.materialize("nope")


class TestSelectiveRead(RuntimeFixture):
    def test_range_read_equals_the_materialised_slice(self) -> None:
        # The contract, asserted directly: read_range(uid, o, l) ==
        # materialize(uid)[o:o+l], for every shape of window.
        for uid, data in self.units.items():
            for offset in (0, 1, 999, 60_000, len(data) - 1, len(data), len(data) + 50):
                for length in (0, 1, 256, 5000):
                    with self.subTest(uid=uid, offset=offset, length=length):
                        result = self.rt.read_range(uid, offset, length)
                        self.assertEqual(result.data, data[offset : offset + length])

    def test_selective_read_touches_far_less_than_the_unit(self) -> None:
        derived = [u for u in self.rt.units() if self.rt.stat(u).kind == "derived"]
        self.assertTrue(derived, "fixture should produce derived units")
        result = self.rt.read_range(derived[0], 40_000, 256)
        self.assertEqual(len(result.data), 256)
        self.assertLess(result.bytes_touched, result.unit_size // 10)
        self.assertLess(result.instructions_visited, result.instructions_total)
        ratio = result.work_ratio
        self.assertIsNotNone(ratio)
        assert ratio is not None
        self.assertLess(ratio, 0.1)

    def test_reading_the_whole_unit_touches_the_whole_unit(self) -> None:
        uid = self.rt.units()[0]
        size = self.rt.stat(uid).size
        result = self.rt.read_range(uid, 0, size)
        self.assertEqual(result.bytes_touched, size)
        self.assertEqual(result.work_ratio, 1.0)

    def test_negative_arguments_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.rt.read_range(self.rt.units()[0], -1, 10)
        with self.assertRaises(ValueError):
            self.rt.read_range(self.rt.units()[0], 0, -10)

    def test_unknown_unit_rejected(self) -> None:
        with self.assertRaises(UnknownUnitError):
            self.rt.read_range("nope", 0, 10)


class TestDeterminism(RuntimeFixture):
    def test_repeated_reads_are_identical(self) -> None:
        uid = self.rt.units()[3]
        first = self.rt.read_range(uid, 1234, 777)
        for _ in range(5):
            again = self.rt.read_range(uid, 1234, 777)
            self.assertEqual(again.data, first.data)
            self.assertEqual(again.bytes_touched, first.bytes_touched)
            self.assertEqual(again.instructions_visited, first.instructions_visited)

    def test_result_does_not_depend_on_preceding_requests(self) -> None:
        uid = self.rt.units()[2]
        clean = open_representation(self.container).read_range(uid, 5000, 300).data
        busy_rt = open_representation(self.container)
        for other in busy_rt.units():
            busy_rt.read_range(other, 0, 4096)
        self.assertEqual(busy_rt.read_range(uid, 5000, 300).data, clean)

    def test_two_runtimes_agree(self) -> None:
        a = open_representation(self.container)
        b = open_representation(self.container)
        for uid in a.units():
            self.assertEqual(a.read_range(uid, 700, 900).data, b.read_range(uid, 700, 900).data)


class TestReadMany(RuntimeFixture):
    def test_results_come_back_in_request_order(self) -> None:
        requests = [(uid, 1000, 128) for uid in reversed(self.rt.units())]
        results = self.rt.read_many(requests)
        self.assertEqual(len(results), len(requests))
        for (uid, offset, length), result in zip(requests, results):
            self.assertEqual(result.uid, uid)
            self.assertEqual(result.data, self.units[uid][offset : offset + length])

    def test_matches_individual_reads(self) -> None:
        requests = [(uid, 2048, 512) for uid in self.rt.units()]
        batched = self.rt.read_many(requests)
        singles = [self.rt.read_range(uid, 2048, 512) for uid, _, _ in requests]
        self.assertEqual([r.data for r in batched], [r.data for r in singles])

    def test_empty_batch(self) -> None:
        self.assertEqual(self.rt.read_many([]), [])


class TestAccounting(RuntimeFixture):
    def test_ledger_totals_match_the_operations(self) -> None:
        self.rt.reset_ledger()
        uid = self.rt.units()[1]
        a = self.rt.read_range(uid, 0, 1000)
        b = self.rt.read_range(uid, 5000, 1000)
        ledger = self.rt.ledger
        self.assertEqual(ledger.operations, 2)
        self.assertEqual(ledger.bytes_touched, a.bytes_touched + b.bytes_touched)
        self.assertEqual(ledger.bytes_returned, a.bytes_returned + b.bytes_returned)
        self.assertEqual(
            ledger.instructions_visited,
            a.instructions_visited + b.instructions_visited,
        )

    def test_materialize_touches_the_whole_unit(self) -> None:
        self.rt.reset_ledger()
        uid = self.rt.units()[0]
        self.rt.materialize(uid)
        self.assertEqual(self.rt.ledger.bytes_touched, self.rt.stat(uid).size)

    def test_selective_read_is_cheaper_in_the_ledger(self) -> None:
        # The measurement the runtime exists to make: same bytes returned, far
        # fewer bytes touched than reconstructing the unit to slice it.
        derived = [u for u in self.rt.units() if self.rt.stat(u).kind == "derived"]
        uid = derived[0]

        self.rt.reset_ledger()
        self.rt.read_range(uid, 30_000, 512)
        selective = self.rt.ledger.bytes_touched

        self.rt.reset_ledger()
        self.rt.materialize(uid)
        full = self.rt.ledger.bytes_touched

        self.assertLess(selective, full)
        self.assertLess(selective * 20, full)

    def test_reset_clears_totals(self) -> None:
        self.rt.read_range(self.rt.units()[0], 0, 100)
        self.rt.reset_ledger()
        self.assertEqual(self.rt.ledger.operations, 0)
        self.assertEqual(self.rt.ledger.bytes_touched, 0)

    def test_record_tail_is_bounded(self) -> None:
        ledger = self.rt.ledger
        ledger.max_records = 8
        for index in range(40):
            self.rt.read_range(self.rt.units()[0], index * 10, 16)
        self.assertLessEqual(len(ledger.records), 8)
        self.assertGreaterEqual(ledger.operations, 40)


class TestInfoAndGroups(RuntimeFixture):
    def test_info_accounting_adds_up(self) -> None:
        info = self.rt.info()
        self.assertEqual(info.units, len(self.units))
        self.assertEqual(info.literals + info.derived, info.units)
        self.assertEqual(info.payload_bytes + info.index_bytes, info.container_bytes)
        self.assertEqual(info.original_bytes, sum(len(v) for v in self.units.values()))
        saving = info.saving
        self.assertIsNotNone(saving)
        assert saving is not None
        self.assertGreater(saving, 0.5)

    def test_stat_fields_are_consistent(self) -> None:
        for uid in self.rt.units():
            info = self.rt.stat(uid)
            self.assertEqual(info.copied_bytes + info.added_bytes, info.size)
            if info.kind == "derived":
                self.assertIsNotNone(info.base_uid)
                self.assertLess(info.stored_bytes, info.size)

    def test_groups_reference_full_stored_bases(self) -> None:
        for base_uid, members in self.rt.groups().items():
            self.assertEqual(self.rt.stat(base_uid).kind, "literal")
            self.assertTrue(members)

    def test_units_equal_without_materialising(self) -> None:
        container = build.from_units({"a": b"same", "b": b"same", "c": b"other"})
        rt = open_representation(container)
        self.assertIs(rt.units_equal("a", "b"), True)
        self.assertIs(rt.units_equal("a", "c"), False)


class TestContract(unittest.TestCase):
    def test_contract_declares_its_operations(self) -> None:
        self.assertTrue(CONTRACT.supports("read_range"))
        self.assertTrue(CONTRACT.supports("materialize"))
        self.assertFalse(CONTRACT.supports("mutate"))
        self.assertFalse(CONTRACT.supports("execute_target_program"))

    def test_contract_renders(self) -> None:
        text = describe_contract()
        self.assertIn("bit-exact", text)
        self.assertIn("not supported yet", text)
        self.assertIn("open design questions", text)


class TestDirectoryBuild(unittest.TestCase):
    def test_build_from_directory_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            expected = {}
            body = deterministic_bytes(40_000, seed=61)
            for index in range(4):
                name = f"v{index}/file.bin"
                path = os.path.join(tmp, name)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                patch = deterministic_bytes(128, seed=61 + index)
                data = body[:1000] + patch + body[1128:]
                with open(path, "wb") as handle:
                    handle.write(data)
                expected[name] = data

            rt = open_representation(build.from_directory(tmp), verify=True)
            self.assertEqual(sorted(rt.units()), sorted(expected))
            for uid, data in expected.items():
                self.assertEqual(rt.materialize(uid), data)
                self.assertEqual(rt.read_range(uid, 500, 200).data, data[500:700])


if __name__ == "__main__":
    unittest.main()
