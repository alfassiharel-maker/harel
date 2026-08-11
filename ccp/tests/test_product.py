"""Tests for the CCP Product Engine.

    python3 -m unittest discover -s ccp/tests -t .

These exercise the product layer without weakening anything below it: the engine
is driven only through its public methods, and the central assertion is unchanged
from the runtime tests — a selective read equals the corresponding slice of a
full materialisation, exactly. Nothing asserts on elapsed time.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest

from ccp.product import (
    BatchReadResult,
    CCPArtifact,
    InvalidRangeError,
    MalformedArtifactError,
    ProductEngine,
    ProductError,
    ResourceError,
    UnitNotFoundError,
    UnsupportedOperationError,
    VerificationError,
    VerificationState,
)


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
    body = deterministic_bytes(size, seed)
    units = {"unit-00": body}
    for index in range(1, count):
        patch = deterministic_bytes(256, seed + index)
        cut = (index * 977) % (size - 256)
        units["unit-%02d" % index] = body[:cut] + patch + body[cut + 256 :]
    return units


class ProductFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = ProductEngine()
        self.units = near_duplicate_units(8, 120_000, seed=71)
        self.container = self.engine.build_from_units(self.units)


class TestLifecycle(ProductFixture):
    def test_open_inspect_close(self) -> None:
        artifact = self.engine.open(self.container)
        self.assertTrue(artifact.is_open)
        self.assertEqual(sorted(artifact.units()), sorted(self.units))
        self.assertEqual(len(artifact), len(self.units))
        artifact.close()
        self.assertFalse(artifact.is_open)

    def test_context_manager_closes(self) -> None:
        with self.engine.open(self.container) as artifact:
            self.assertTrue(artifact.is_open)
            inner = artifact
        self.assertFalse(inner.is_open)

    def test_operations_after_close_raise_resource_error(self) -> None:
        artifact = self.engine.open(self.container)
        artifact.close()
        for call in (
            lambda: artifact.units(),
            lambda: artifact.info(),
            lambda: artifact.materialize("unit-00"),
            lambda: artifact.read_range("unit-00", 0, 10),
            lambda: artifact.validate(),
        ):
            with self.assertRaises(ResourceError):
                call()

    def test_close_is_idempotent(self) -> None:
        artifact = self.engine.open(self.container)
        artifact.close()
        artifact.close()  # must not raise
        self.assertFalse(artifact.is_open)

    def test_create_from_units_opens_a_usable_artifact(self) -> None:
        with self.engine.create_from_units(self.units) as artifact:
            self.assertTrue(artifact.is_verified)  # create verifies by default
            for uid, data in self.units.items():
                self.assertEqual(artifact.materialize(uid).data, data)

    def test_create_from_directory_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            expected = {}
            body = deterministic_bytes(40_000, seed=81)
            for index in range(4):
                name = f"v{index}/file.bin"
                path = os.path.join(tmp, name)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                data = body[:800] + deterministic_bytes(96, 81 + index) + body[896:]
                with open(path, "wb") as handle:
                    handle.write(data)
                expected[name] = data
            out = os.path.join(tmp, "artifact.ccp")
            with self.engine.create_from_directory(tmp, save_to=out) as artifact:
                self.assertTrue(os.path.isfile(out))
                for uid, data in expected.items():
                    self.assertEqual(artifact.materialize(uid).data, data)
            # reopen the persisted artifact
            with self.engine.open(out, verify=True) as reopened:
                self.assertEqual(sorted(reopened.units()), sorted(expected))


class TestErrors(ProductFixture):
    def test_malformed_artifact(self) -> None:
        with self.assertRaises(MalformedArtifactError):
            self.engine.open(b"this is not a ccp container")

    def test_truncated_artifact(self) -> None:
        with self.assertRaises(MalformedArtifactError):
            self.engine.open(self.container[: len(self.container) // 3])

    def test_all_product_errors_are_product_error(self) -> None:
        with self.assertRaises(ProductError):
            self.engine.open(b"nope")

    def test_invalid_uid(self) -> None:
        with self.engine.open(self.container) as artifact:
            with self.assertRaises(UnitNotFoundError) as ctx:
                artifact.materialize("does-not-exist")
            self.assertEqual(ctx.exception.uid, "does-not-exist")
            with self.assertRaises(UnitNotFoundError):
                artifact.read_range("does-not-exist", 0, 10)
            with self.assertRaises(UnitNotFoundError):
                artifact.stat("does-not-exist")

    def test_invalid_range(self) -> None:
        with self.engine.open(self.container) as artifact:
            with self.assertRaises(InvalidRangeError):
                artifact.read_range("unit-00", -1, 10)
            with self.assertRaises(InvalidRangeError):
                artifact.read_range("unit-00", 0, -5)

    def test_verification_failure_maps_to_verification_error(self) -> None:
        corrupted = bytearray(self.container)
        corrupted[len(corrupted) - 48] ^= 0xFF
        with self.assertRaises((VerificationError, MalformedArtifactError)):
            self.engine.open(bytes(corrupted), verify=True)

    def test_unsupported_operation(self) -> None:
        with self.engine.open(self.container) as artifact:
            self.assertFalse(artifact.contract_supports("mutate"))
            with self.assertRaises(UnsupportedOperationError) as ctx:
                artifact.require_supported("mutate")
            self.assertEqual(ctx.exception.operation, "mutate")
            # a supported operation does not raise
            artifact.require_supported("read_range")

    def test_resource_error_for_oversize_artifact(self) -> None:
        small_engine = ProductEngine(max_artifact_bytes=32)
        with self.assertRaises(ResourceError):
            small_engine.open(self.container)

    def test_resource_error_for_missing_file(self) -> None:
        with self.assertRaises(ResourceError):
            self.engine.open("/nonexistent/path/model.ccp")

    def test_original_cause_is_preserved(self) -> None:
        try:
            self.engine.open(b"nope")
        except MalformedArtifactError as error:
            self.assertIsNotNone(error.cause)
        else:
            self.fail("expected MalformedArtifactError")


class TestValidation(ProductFixture):
    def test_validate_passes_on_a_good_artifact(self) -> None:
        with self.engine.open(self.container) as artifact:
            report = artifact.validate()
            self.assertTrue(report.ok)
            self.assertEqual(report.units_ok, len(self.units))
            self.assertTrue(artifact.is_verified)


class TestExactness(ProductFixture):
    def test_materialize_is_bit_exact(self) -> None:
        with self.engine.open(self.container) as artifact:
            for uid, data in self.units.items():
                self.assertEqual(artifact.materialize(uid).data, data)

    def test_read_range_equals_materialized_slice(self) -> None:
        with self.engine.open(self.container) as artifact:
            for uid, data in self.units.items():
                for offset in (0, 1, 5000, 60_000, len(data) - 1, len(data), len(data) + 9):
                    for length in (0, 1, 256, 4096):
                        with self.subTest(uid=uid, offset=offset, length=length):
                            result = artifact.read_range(uid, offset, length)
                            self.assertEqual(result.data, data[offset : offset + length])


class TestSelectiveAccess(ProductFixture):
    def test_small_read_touches_far_less_than_the_unit(self) -> None:
        with self.engine.open(self.container) as artifact:
            derived = [u for u in artifact.units() if artifact.stat(u).kind == "derived"]
            self.assertTrue(derived)
            result = artifact.read_range(derived[0], 40_000, 256)
            self.assertEqual(len(result.data), 256)
            self.assertLess(result.report.bytes_touched, result.report.bytes_returned * 4 + 8192)
            self.assertLess(result.report.instructions_visited, result.report.instructions_total)

    def test_selective_read_never_materialises_the_whole_artifact(self) -> None:
        # The load-bearing product guarantee: a windowed read touches only a
        # fraction of the total bytes in the artifact.
        with self.engine.open(self.container) as artifact:
            total = artifact.info().original_bytes
            artifact.reset_ledger()
            for uid in artifact.units():
                size = artifact.stat(uid).size
                if size:
                    artifact.read_range(uid, size // 2, 128)
            self.assertLess(artifact.ledger.bytes_touched, total // 5)

    def test_materialize_touches_whole_unit(self) -> None:
        with self.engine.open(self.container) as artifact:
            uid = artifact.units()[0]
            result = artifact.materialize(uid)
            self.assertEqual(result.report.bytes_touched, artifact.stat(uid).size)
            self.assertEqual(result.report.verification, VerificationState.DIGEST_CHECKED)

    def test_read_range_reports_slice_unverified(self) -> None:
        with self.engine.open(self.container) as artifact:
            result = artifact.read_range(artifact.units()[0], 0, 100)
            self.assertEqual(result.report.verification, VerificationState.SLICE_UNVERIFIED)


class TestBatch(ProductFixture):
    def test_read_many_in_request_order(self) -> None:
        with self.engine.open(self.container) as artifact:
            requests = [(uid, 1000, 128) for uid in reversed(artifact.units())]
            batch = artifact.read_many(requests)
            self.assertIsInstance(batch, BatchReadResult)
            self.assertEqual(len(batch.reads), len(requests))
            for (uid, offset, length), read in zip(requests, batch.reads):
                self.assertEqual(read.uid, uid)
                self.assertEqual(read.data, self.units[uid][offset : offset + length])

    def test_batch_matches_individual_reads(self) -> None:
        with self.engine.open(self.container) as artifact:
            requests = [(uid, 2048, 512) for uid in artifact.units()]
            batch = artifact.read_many(requests)
            singles = [artifact.read_range(uid, 2048, 512).data for uid, _, _ in requests]
            self.assertEqual([r.data for r in batch.reads], singles)

    def test_batch_aggregate_report(self) -> None:
        with self.engine.open(self.container) as artifact:
            requests = [(uid, 0, 256) for uid in artifact.units()]
            batch = artifact.read_many(requests)
            self.assertEqual(batch.report.units_touched, len(set(u for u, _, _ in requests)))
            self.assertEqual(
                batch.report.bytes_touched,
                sum(r.report.bytes_touched for r in batch.reads),
            )

    def test_empty_batch(self) -> None:
        with self.engine.open(self.container) as artifact:
            batch = artifact.read_many([])
            self.assertEqual(batch.reads, ())


class TestObservability(ProductFixture):
    def test_report_fields_present(self) -> None:
        with self.engine.open(self.container) as artifact:
            result = artifact.read_range(artifact.units()[0], 100, 256)
            report = result.report
            self.assertEqual(report.bytes_requested, 256)
            self.assertGreaterEqual(report.bytes_returned, 0)
            self.assertGreaterEqual(report.bytes_touched, report.bytes_returned)
            self.assertEqual(report.units_touched, 1)
            self.assertGreaterEqual(report.instructions_visited, 0)
            self.assertGreaterEqual(report.elapsed_seconds, 0.0)

    def test_ledger_totals_match(self) -> None:
        with self.engine.open(self.container) as artifact:
            uid = artifact.units()[1]
            a = artifact.read_range(uid, 0, 1000)
            b = artifact.read_range(uid, 5000, 1000)
            self.assertEqual(artifact.ledger.operations, 2)
            self.assertEqual(
                artifact.ledger.bytes_touched,
                a.report.bytes_touched + b.report.bytes_touched,
            )
            self.assertEqual(
                artifact.ledger.bytes_returned,
                a.report.bytes_returned + b.report.bytes_returned,
            )

    def test_work_ratio_below_one_for_a_small_window(self) -> None:
        with self.engine.open(self.container) as artifact:
            derived = [u for u in artifact.units() if artifact.stat(u).kind == "derived"]
            result = artifact.read_range(derived[0], 30_000, 64)
            ratio = result.report.work_ratio
            self.assertIsNotNone(ratio)
            assert ratio is not None
            # touched should be near the window, so the ratio is small.
            self.assertLess(ratio, 200.0)

    def test_reset_ledger(self) -> None:
        with self.engine.open(self.container) as artifact:
            artifact.read_range(artifact.units()[0], 0, 10)
            artifact.reset_ledger()
            self.assertEqual(artifact.ledger.operations, 0)
            self.assertEqual(artifact.ledger.bytes_touched, 0)

    def test_no_shared_state_between_artifacts(self) -> None:
        a = self.engine.open(self.container)
        b = self.engine.open(self.container)
        a.read_range(a.units()[0], 0, 100)
        self.assertEqual(b.ledger.operations, 0)
        a.close()
        b.close()


class TestDeterminism(ProductFixture):
    def test_repeated_reads_identical(self) -> None:
        with self.engine.open(self.container) as artifact:
            uid = artifact.units()[3]
            first = artifact.read_range(uid, 1234, 777)
            for _ in range(5):
                again = artifact.read_range(uid, 1234, 777)
                self.assertEqual(again.data, first.data)
                self.assertEqual(again.report.bytes_touched, first.report.bytes_touched)

    def test_output_independent_of_request_order(self) -> None:
        uid = "unit-04"
        clean = self.engine.open(self.container)
        expected = clean.read_range(uid, 5000, 300).data
        clean.close()

        busy = self.engine.open(self.container)
        for other in busy.units():
            busy.read_range(other, 0, 4096)
        self.assertEqual(busy.read_range(uid, 5000, 300).data, expected)
        busy.close()

    def test_two_engines_agree(self) -> None:
        e1 = ProductEngine()
        e2 = ProductEngine()
        with e1.open(self.container) as a, e2.open(self.container) as b:
            for uid in a.units():
                self.assertEqual(
                    a.read_range(uid, 700, 900).data, b.read_range(uid, 700, 900).data
                )


class TestConcurrencyIsOpen(unittest.TestCase):
    """Concurrency is not tested as supported, because the contract does not
    permit it. This test documents that decision as an executable statement so it
    cannot be silently forgotten: the contract lists concurrent access to one
    instance as unsupported, and the product marks it OPEN rather than providing
    it half-way."""

    def test_contract_marks_concurrency_unsupported(self) -> None:
        from ccp.api import CONTRACT

        joined = " ".join(CONTRACT.unsupported_operations).lower()
        self.assertIn("concurrent", joined)


if __name__ == "__main__":
    unittest.main()
