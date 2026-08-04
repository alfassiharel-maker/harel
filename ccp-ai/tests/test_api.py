"""API tests, driven against the Api object rather than over a socket.

Includes the tenant-isolation matrix: for every endpoint that touches tenant
state, a test that tenant A's request cannot observe or alter tenant B's models.
A new endpoint without a row here is an incomplete endpoint.
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import sys
import tempfile
import time
import unittest
from array import array

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import safetensors
from server.app import Api
from store import IdentifierError, RepositoryError


def f32(values) -> bytes:
    a = array("f", values)
    if sys.byteorder == "big":
        a.byteswap()
    return a.tobytes()


def blob(scale: float = 1.0, step: float = 0.0) -> bytes:
    return safetensors.build(
        [
            ("embed.weight", "F32", (64, 32), f32([i * 0.001 * scale * (1 + step) for i in range(2048)])),
            ("layer.weight", "F32", (32, 32), f32([i * 0.002 * scale * (1 + step) for i in range(1024)])),
        ],
        metadata={"format": "pt"},
    )


class Headers(dict):
    """Stand-in for the handler's header mapping."""

    def get(self, key, default=None):  # noqa: D102 — dict-like on purpose
        for existing, value in self.items():
            if existing.lower() == key.lower():
                return value
        return default


class ApiCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="ccp-api-")
        self.api = Api(store_root=self.root)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def populate(self, tenant: str, variants: int = 2, scale: float = 1.0) -> None:
        repo = self.api.registry.get(tenant)
        repo.set_base(blob(scale), base_id="base", label="base")
        for i in range(variants):
            repo.add_variant(blob(scale, step=1e-4 * (i + 1)), variant_id=f"v{i}", label=f"variant {i}")


class HealthAndStateTests(ApiCase):
    def test_health(self) -> None:
        payload = self.api.health()
        self.assertEqual(payload["status"], "ok")
        self.assertIn("engine_version", payload)

    def test_state_of_an_empty_tenant_reports_nothing_measured(self) -> None:
        state = self.api.state("demo")
        self.assertFalse(state["seeded"])
        self.assertIsNone(state["projection"])
        self.assertIsNone(state["repository"]["variant_ratio"])

    def test_state_after_population(self) -> None:
        self.populate("demo", variants=3)
        state = self.api.state("demo")
        self.assertTrue(state["seeded"])
        self.assertTrue(state["ccp_enabled"])
        self.assertEqual(state["repository"]["variant_count"], 3)
        self.assertIsNotNone(state["projection"])
        self.assertGreater(state["costs"]["disk_usd_month_if_full"], state["costs"]["disk_usd_month_if_ccp"])


class ModeTests(ApiCase):
    def test_switch_moves_bytes_and_reports_both_sides(self) -> None:
        self.populate("demo")
        off = self.api.set_mode("demo", False)
        self.assertEqual(off["switch"]["mode"], "full")
        self.assertGreater(off["switch"]["disk_bytes_after"], off["switch"]["disk_bytes_before"])

        on = self.api.set_mode("demo", True)
        self.assertEqual(on["switch"]["mode"], "ccp")
        self.assertLess(on["switch"]["disk_bytes_after"], on["switch"]["disk_bytes_before"])
        self.assertEqual(on["switch"]["disk_bytes_after"], off["switch"]["disk_bytes_before"])

    def test_verification_passes_in_both_modes(self) -> None:
        self.populate("demo", variants=2)
        for enabled in (False, True, False):
            self.api.set_mode("demo", enabled)
            report = self.api.verify("demo")
            self.assertTrue(report["all_lossless"], f"mode enabled={enabled}")


class ProjectionEndpointTests(ApiCase):
    def test_projection_requires_a_measurement(self) -> None:
        with self.assertRaises(RepositoryError):
            self.api.projection("demo", params_billions=7, variants=10, downloads=1000)

    def test_projection_uses_the_measured_ratio(self) -> None:
        self.populate("demo")
        measured = self.api.registry.get("demo").variant_ratio()
        payload = self.api.projection("demo", params_billions=7, variants=10, downloads=1000)
        self.assertAlmostEqual(payload["assumptions"]["measured_variant_ratio"], measured, places=12)


class InspectTests(ApiCase):
    def test_inspect_needs_a_recorded_plan(self) -> None:
        self.populate("demo")
        with self.assertRaises(RepositoryError):
            self.api.inspect("demo", "v0")

    def test_seed_records_plans_that_inspect_can_read(self) -> None:
        from demo.generate_models import Arch

        job = self.api.seed("demo", Arch(d_model=32, n_layers=1, vocab=256), reset=True)
        self.assertEqual(job["state"], "running")
        deadline = time.time() + 120
        while self.api.jobs.busy("demo") and time.time() < deadline:
            time.sleep(0.2)
        current = self.api.jobs.current("demo")
        self.assertEqual(current.state, "done", current.error)

        state = self.api.state("demo")
        self.assertTrue(state["seeded"])
        self.assertGreater(state["repository"]["variant_count"], 0)

        first = state["repository"]["variants"][0]["variant_id"]
        detail = self.api.inspect("demo", first)
        self.assertIn("tensors", detail)
        self.assertIn("by_op", detail)

    def test_inspect_rejects_a_traversal_id(self) -> None:
        self.populate("demo")
        with self.assertRaises(IdentifierError):
            self.api.inspect("demo", "../other/v0")


class AuthTests(ApiCase):
    def test_no_token_configured_allows_everything(self) -> None:
        self.assertTrue(self.api.authorised(None))

    def test_token_required_when_configured(self) -> None:
        api = Api(store_root=self.root, token="s3cret")
        self.assertFalse(api.authorised(None))
        self.assertFalse(api.authorised("Bearer wrong"))
        self.assertFalse(api.authorised("s3cret"))  # missing the scheme
        self.assertTrue(api.authorised("Bearer s3cret"))

    def test_tenant_resolution_normalises_and_validates(self) -> None:
        self.assertEqual(self.api.tenant_of(Headers(), {}), "demo")
        self.assertEqual(self.api.tenant_of(Headers({"X-CCP-Tenant": " ACME "}), {}), "acme")
        self.assertEqual(self.api.tenant_of(Headers(), {"tenant": ["globex"]}), "globex")
        with self.assertRaises(IdentifierError):
            self.api.tenant_of(Headers({"X-CCP-Tenant": "../etc"}), {})


class TenantIsolationMatrixTests(ApiCase):
    """One row per tenant-touching endpoint."""

    def setUp(self) -> None:
        super().setUp()
        self.populate("acme", variants=2, scale=1.0)
        self.populate("globex", variants=1, scale=7.0)

    def test_state_is_scoped(self) -> None:
        acme = self.api.state("acme")["repository"]
        globex = self.api.state("globex")["repository"]
        self.assertEqual(acme["variant_count"], 2)
        self.assertEqual(globex["variant_count"], 1)
        self.assertNotEqual(acme["base"]["digest"], globex["base"]["digest"])

    def test_mode_switch_is_scoped(self) -> None:
        self.api.set_mode("acme", False)
        self.assertEqual(self.api.state("acme")["repository"]["mode"], "full")
        self.assertEqual(self.api.state("globex")["repository"]["mode"], "ccp")

    def test_verify_is_scoped(self) -> None:
        self.assertEqual(self.api.verify("acme")["variants_checked"], 2)
        self.assertEqual(self.api.verify("globex")["variants_checked"], 1)

    def test_download_is_scoped(self) -> None:
        acme_blob, _ = self.api.download("acme", "v0")
        globex_blob, _ = self.api.download("globex", "v0")
        self.assertNotEqual(acme_blob, globex_blob)

    def test_download_of_a_foreign_variant_id_fails(self) -> None:
        self.api.registry.get("acme").add_variant(blob(1.0, step=5e-4), variant_id="acme-only")
        with self.assertRaises(RepositoryError):
            self.api.download("globex", "acme-only")

    def test_ledger_is_scoped(self) -> None:
        acme_entries = self.api.ledger("acme", 100)["entries"]
        globex_entries = self.api.ledger("globex", 100)["entries"]
        acme_variants = {e["detail"].get("variant_id") for e in acme_entries if e["action"] == "variant.add"}
        globex_variants = {e["detail"].get("variant_id") for e in globex_entries if e["action"] == "variant.add"}
        self.assertEqual(len(acme_variants), 2)
        self.assertEqual(len(globex_variants), 1)

    def test_projection_is_scoped(self) -> None:
        acme = self.api.projection("acme", params_billions=7, variants=10, downloads=100)
        globex = self.api.projection("globex", params_billions=7, variants=10, downloads=100)
        self.assertNotEqual(
            acme["assumptions"]["measured_variant_ratio"], globex["assumptions"]["measured_variant_ratio"]
        )

    def test_reset_is_scoped(self) -> None:
        self.api.registry.get("globex").reset()
        self.assertEqual(self.api.state("acme")["repository"]["variant_count"], 2)
        self.assertEqual(self.api.state("globex")["repository"]["variant_count"], 0)

    def test_tenants_listing_exposes_names_only(self) -> None:
        listing = self.api.registry.tenants()
        self.assertEqual(sorted(listing), ["acme", "globex"])
        self.assertTrue(all(isinstance(name, str) for name in listing))


class JobTests(ApiCase):
    def test_one_job_per_tenant(self) -> None:
        from demo.generate_models import Arch

        from server.jobs import JobBusy

        arch = Arch(d_model=32, n_layers=1, vocab=256)
        self.api.seed("demo", arch, reset=True)
        with self.assertRaises(JobBusy):
            self.api.seed("demo", arch, reset=True)
        # A different tenant is unaffected — the lock is per tenant, not global.
        self.api.seed("other", arch, reset=True)

        deadline = time.time() + 180
        while (self.api.jobs.busy("demo") or self.api.jobs.busy("other")) and time.time() < deadline:
            time.sleep(0.2)
        self.assertEqual(self.api.jobs.current("demo").state, "done")
        self.assertEqual(self.api.jobs.current("other").state, "done")

    def test_mode_switch_refused_while_a_job_runs(self) -> None:
        from demo.generate_models import Arch

        from server.jobs import JobBusy

        self.api.seed("demo", Arch(d_model=32, n_layers=1, vocab=256), reset=True)
        try:
            with self.assertRaises(JobBusy):
                self.api.set_mode("demo", False)
        finally:
            deadline = time.time() + 120
            while self.api.jobs.busy("demo") and time.time() < deadline:
                time.sleep(0.2)

    def test_failed_job_records_the_error_without_a_stack_trace(self) -> None:
        def explode(job):
            raise ValueError("nope")

        # The runner prints the trace to stderr by design; swallow it here so a
        # deliberate failure does not look like a broken test run.
        with contextlib.redirect_stderr(io.StringIO()):
            job = self.api.jobs.submit("demo", "test", explode)
            deadline = time.time() + 10
            while job.state == "running" and time.time() < deadline:
                time.sleep(0.05)
        deadline = time.time() + 10
        while job.state == "running" and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(job.state, "failed")
        self.assertEqual(job.error, "ValueError: nope")
        self.assertNotIn("Traceback", job.error)


if __name__ == "__main__":
    unittest.main()
