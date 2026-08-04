"""Store tests: the switch really moves bytes, and tenants cannot see each other.

The isolation tests are not hypothetical hardening. This service holds other
companies' unreleased model weights; a cross-tenant read is the one bug that ends
the company. Every new store method gets a row here.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from array import array

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import safetensors
from engine.codec import sha256
from store import IdentifierError, Repository, RepositoryError, RepositoryRegistry, validate_id


def f32(values) -> bytes:
    a = array("f", values)
    if sys.byteorder == "big":
        a.byteswap()
    return a.tobytes()


def base_blob(scale: float = 1.0) -> bytes:
    return safetensors.build(
        [
            ("embed.weight", "F32", (128, 32), f32([i * 0.001 * scale for i in range(4096)])),
            ("layer.weight", "F32", (32, 32), f32([i * 0.002 * scale for i in range(1024)])),
        ],
        metadata={"format": "pt"},
    )


def variant_blob(step: float, scale: float = 1.0) -> bytes:
    return safetensors.build(
        [
            ("embed.weight", "F32", (128, 32), f32([i * 0.001 * scale * (1 + step) for i in range(4096)])),
            ("layer.weight", "F32", (32, 32), f32([i * 0.002 * scale * (1 + step) for i in range(1024)])),
        ],
        metadata={"format": "pt"},
    )


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="ccp-test-")
        self.registry = RepositoryRegistry(self.root)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def seeded(self, tenant: str = "acme", variants: int = 2) -> Repository:
        repo = self.registry.get(tenant)
        repo.set_base(base_blob(), base_id="base", label="base")
        for i in range(variants):
            repo.add_variant(variant_blob(1e-4 * (i + 1)), variant_id=f"v{i}", label=f"variant {i}")
        return repo


class IdentifierTests(unittest.TestCase):
    def test_valid_ids_accepted(self) -> None:
        for value in ("demo", "acme-corp", "a", "model.v2", "x_1"):
            self.assertEqual(validate_id(value, "tenant id"), value)

    def test_traversal_and_separators_rejected(self) -> None:
        for value in ("../etc", "a/b", "..", "/abs", "A-Upper", "", "-lead", "x" * 64, "tenant\x00", "té"):
            with self.subTest(value=value), self.assertRaises(IdentifierError):
                validate_id(value, "tenant id")


class ModeSwitchTests(StoreCase):
    def test_ccp_mode_stores_less_than_full_mode(self) -> None:
        repo = self.seeded()
        ccp_bytes = repo.disk_bytes()

        result = repo.set_mode("full")
        self.assertTrue(result["changed"])
        full_bytes = repo.disk_bytes()

        self.assertGreater(full_bytes, ccp_bytes)
        self.assertEqual(result["disk_bytes_before"], ccp_bytes)
        self.assertEqual(result["disk_bytes_after"], full_bytes)

    def test_full_mode_writes_real_checkpoints(self) -> None:
        repo = self.seeded()
        repo.set_mode("full")
        for record in repo.records():
            path = os.path.join(repo.root, "variants", f"{record.variant_id}.safetensors")
            self.assertTrue(os.path.isfile(path))
            with open(path, "rb") as fh:
                blob = fh.read()
            self.assertEqual(sha256(blob), record.raw_digest)
            safetensors.parse(blob)  # a real, loadable checkpoint

    def test_round_trip_through_both_modes_is_lossless(self) -> None:
        repo = self.seeded(variants=3)
        expected = {r.variant_id: r.raw_digest for r in repo.records()}
        for mode in ("full", "ccp", "full", "ccp"):
            repo.set_mode(mode)
            for variant_id, digest in expected.items():
                blob, _ = repo.materialise(variant_id)
                self.assertEqual(sha256(blob), digest, f"{variant_id} in mode {mode}")

    def test_setting_the_same_mode_is_a_no_op(self) -> None:
        repo = self.seeded()
        result = repo.set_mode("ccp")
        self.assertFalse(result["changed"])
        self.assertEqual(result["migrated"], 0)

    def test_unknown_mode_refused(self) -> None:
        repo = self.seeded()
        with self.assertRaises(RepositoryError):
            repo.set_mode("cheap")

    def test_mode_survives_a_reopen(self) -> None:
        repo = self.seeded()
        repo.set_mode("full")
        reopened = Repository(self.root, "acme")
        self.assertEqual(reopened.mode, "full")
        self.assertEqual(len(reopened.records()), 2)


class MeasurementTests(StoreCase):
    def test_disk_bytes_matches_the_filesystem(self) -> None:
        repo = self.seeded()
        walked = 0
        for directory, _, names in os.walk(os.path.join(repo.root, "variants")):
            for name in names:
                walked += os.stat(os.path.join(directory, name)).st_size
        walked += os.stat(repo.base_path).st_size
        self.assertEqual(repo.disk_bytes(), walked)

    def test_plans_directory_is_not_counted_as_stored_model_bytes(self) -> None:
        # Metadata about a saving must never inflate the saving.
        repo = self.seeded()
        before = repo.disk_bytes()
        plans = os.path.join(repo.root, "plans")
        os.makedirs(plans, exist_ok=True)
        with open(os.path.join(plans, "noise.json"), "w", encoding="utf-8") as fh:
            fh.write("x" * 50_000)
        self.assertEqual(repo.disk_bytes(), before)

    def test_savings_is_none_when_nothing_is_stored(self) -> None:
        repo = self.registry.get("empty")
        self.assertIsNone(repo.savings().savings_ratio)
        self.assertIsNone(repo.variant_ratio())

    def test_variant_ratio_excludes_the_base(self) -> None:
        repo = self.seeded()
        records = repo.records()
        expected = sum(r.container_bytes for r in records) / sum(r.raw_bytes for r in records)
        self.assertAlmostEqual(repo.variant_ratio(), expected, places=12)

    def test_summary_reports_both_hypothetical_footprints(self) -> None:
        repo = self.seeded()
        summary = repo.summary()
        self.assertEqual(summary["disk_bytes"], summary["disk_bytes_if_ccp"])
        self.assertGreater(summary["disk_bytes_if_full"], summary["disk_bytes_if_ccp"])
        repo.set_mode("full")
        summary = repo.summary()
        self.assertEqual(summary["disk_bytes"], summary["disk_bytes_if_full"])


class VerificationTests(StoreCase):
    def test_verify_all_passes_on_a_healthy_store(self) -> None:
        repo = self.seeded(variants=3)
        report = repo.verify_all()
        self.assertTrue(report["all_lossless"])
        self.assertEqual(report["variants_checked"], 3)
        self.assertTrue(report["ledger_chain_ok"])

    def test_corrupted_container_is_reported_not_served(self) -> None:
        repo = self.seeded(variants=1)
        path = os.path.join(repo.root, "variants", "v0.ccp")
        with open(path, "r+b") as fh:
            fh.seek(-1, os.SEEK_END)
            last = fh.read(1)
            fh.seek(-1, os.SEEK_END)
            fh.write(bytes([last[0] ^ 0xFF]))

        report = repo.verify_all()
        self.assertFalse(report["all_lossless"])

        with self.assertRaises((RepositoryError, Exception)):
            repo.materialise("v0")

    def test_swapped_base_is_detected(self) -> None:
        repo = self.seeded(variants=1)
        with open(repo.base_path, "wb") as fh:
            fh.write(base_blob(scale=1.5))
        report = repo.verify_all()
        self.assertFalse(report["all_lossless"])


class TenantIsolationTests(StoreCase):
    def test_tenants_get_separate_directories(self) -> None:
        a = self.seeded("acme", variants=1)
        b = self.seeded("globex", variants=1)
        self.assertNotEqual(a.root, b.root)
        self.assertTrue(a.root.endswith(os.path.join("tenants", "acme")))
        self.assertTrue(b.root.endswith(os.path.join("tenants", "globex")))

    def test_one_tenant_cannot_name_anothers_variant(self) -> None:
        self.seeded("acme", variants=1)
        globex = self.registry.get("globex")
        globex.set_base(base_blob(scale=2.0), base_id="base")
        with self.assertRaises(RepositoryError):
            globex.materialise("v0")  # acme's variant id

    def test_reset_of_one_tenant_leaves_the_other_intact(self) -> None:
        acme = self.seeded("acme", variants=2)
        globex = self.seeded("globex", variants=2)
        globex.reset()
        self.assertEqual(len(acme.records()), 2)
        self.assertTrue(acme.verify_all()["all_lossless"])

    def test_traversal_in_a_tenant_id_cannot_escape_the_store(self) -> None:
        for evil in ("../../etc", "..", "a/../../b"):
            with self.subTest(evil=evil), self.assertRaises(IdentifierError):
                self.registry.get(evil)

    def test_traversal_in_a_variant_id_cannot_escape_the_tenant(self) -> None:
        repo = self.seeded("acme", variants=1)
        for evil in ("../globex/v0", "/etc/passwd", "..%2fv0"):
            with self.subTest(evil=evil), self.assertRaises(IdentifierError):
                repo.materialise(evil)

    def test_registry_lists_names_only(self) -> None:
        self.seeded("acme", variants=1)
        self.seeded("globex", variants=1)
        self.assertEqual(self.registry.tenants(), ["acme", "globex"])


class LedgerTests(StoreCase):
    def test_every_operation_is_recorded(self) -> None:
        repo = self.seeded(variants=2)
        repo.set_mode("full")
        actions = [e.action for e in repo.ledger.read()]
        self.assertEqual(actions.count("base.set"), 1)
        self.assertEqual(actions.count("variant.add"), 2)
        self.assertEqual(actions.count("mode.set"), 1)

    def test_chain_verifies(self) -> None:
        repo = self.seeded(variants=2)
        ok, error = repo.ledger.verify_chain()
        self.assertTrue(ok, error)

    def test_tampered_entry_breaks_the_chain(self) -> None:
        repo = self.seeded(variants=1)
        with open(repo.ledger.path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
        lines[0] = lines[0].replace('"bytes":', '"BYTES":')
        with open(repo.ledger.path, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
        ok, error = repo.ledger.verify_chain()
        self.assertFalse(ok)
        self.assertIsNotNone(error)

    def test_ledger_survives_a_reset(self) -> None:
        repo = self.seeded(variants=1)
        before = len(repo.ledger.read())
        repo.reset()
        self.assertGreater(len(repo.ledger.read()), before)
        self.assertTrue(repo.ledger.verify_chain()[0])


class BaseTests(StoreCase):
    def test_adding_a_variant_without_a_base_is_refused(self) -> None:
        repo = self.registry.get("acme")
        with self.assertRaises(RepositoryError):
            repo.add_variant(variant_blob(1e-4), variant_id="v0")

    def test_replacing_the_base_clears_variants(self) -> None:
        repo = self.seeded(variants=2)
        repo.set_base(base_blob(scale=3.0), base_id="base2")
        self.assertEqual(repo.records(), [])
        self.assertEqual(os.listdir(os.path.join(repo.root, "variants")), [])

    def test_malformed_upload_is_rejected_before_it_lands(self) -> None:
        repo = self.registry.get("acme")
        with self.assertRaises(safetensors.SafetensorsError):
            repo.set_base(b"not a checkpoint at all", base_id="base")
        self.assertFalse(os.path.exists(repo.base_path))


if __name__ == "__main__":
    unittest.main()
