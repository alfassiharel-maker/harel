"""End-to-end tests for the CCP Forge application.

    python3 -m unittest discover -s ccp/tests -t .

Two levels, both real:

*   the **controller**, driven directly — the whole product workflow without a
    display, which is why the application is testable at all in a headless
    environment;
*   the **HTTP surface**, driven over a real socket exactly as the browser does,
    including the session token, so the transport is covered too.

The central assertion is the same one that anchors every layer below: a selective
read equals the corresponding slice of a full materialisation, byte for byte.
Nothing asserts on elapsed time.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request

from ccp.integration import InputLimits, RecentArtifacts
from ccp.integration.errors import InputError
from ccp.product import (
    InvalidRangeError,
    MalformedArtifactError,
    ProductError,
    ResourceError,
    UnitNotFoundError,
    UnsupportedOperationError,
    VerificationError,
)
from ccp.ui.controller import AppController, AppState, describe_error
from ccp.ui.server import AppServer


def deterministic_bytes(size: int, seed: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < size:
        out += hashlib.blake2b(
            counter.to_bytes(8, "little") + seed.to_bytes(8, "little"), digest_size=64
        ).digest()
        counter += 1
    return bytes(out[:size])


def make_project(root: str, versions: int = 4, size: int = 200_000) -> dict:
    """Several near-identical revisions of a project — the shape CCP is for.

    Units are large enough, and each revision differs in two separate places, so
    a unit's change program has several instructions. That matters: with a
    single-instruction unit there is no difference between visiting some
    instructions and visiting all of them, and the selective-access assertions
    would pass without testing anything.
    """
    body = deterministic_bytes(size, seed=11)
    midpoint = size // 2
    expected = {}
    for index in range(versions):
        for name in ("service.py", "models.py"):
            relative = f"v{index}/{name}"
            path = os.path.join(root, f"v{index}", name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            head = deterministic_bytes(96, seed=200 + index)
            tail = deterministic_bytes(96, seed=300 + index)
            data = (
                body[:1000]
                + head
                + body[1096:midpoint]
                + tail
                + body[midpoint + 96 :]
            )
            with open(path, "wb") as handle:
                handle.write(data)
            expected[relative] = data
    return expected


class AppFixture(unittest.TestCase):
    """A built artifact in a throwaway workspace, torn down after each test."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.project = os.path.join(self.tmp, "project")
        os.makedirs(self.project)
        self.expected = make_project(self.project)
        self.controller = AppController(
            recents=RecentArtifacts(os.path.join(self.tmp, "appdata")),
            output_dir=os.path.join(self.tmp, "out"),
        )

    def tearDown(self) -> None:
        self.controller.close()
        self._tmp.cleanup()


class TestBuildWorkflow(AppFixture):
    def test_build_analyses_represents_and_verifies(self) -> None:
        self.assertIs(self.controller.state, AppState.NO_INPUT)
        summary = self.controller.build(self.project)
        self.assertIs(self.controller.state, AppState.READY)
        self.assertEqual(summary.units, len(self.expected))
        self.assertTrue(summary.verified)
        self.assertIsNotNone(summary.saving)
        assert summary.saving is not None
        self.assertGreater(summary.saving, 0.0)
        self.assertGreater(summary.groups, 0)

    def test_analyse_only_builds_nothing(self) -> None:
        snapshot = self.controller.analyse(self.project)
        self.assertIsNone(snapshot["artifact"])
        self.assertEqual(snapshot["analysis"]["unit_count"], len(self.expected))

    def test_async_build_reports_progress_and_completes(self) -> None:
        self.controller.build_async(self.project)
        self.assertTrue(self.controller.wait_for_build(120))
        self.assertIs(self.controller.state, AppState.READY)
        snapshot = self.controller.snapshot()
        self.assertFalse(snapshot["status"]["running"])
        self.assertEqual(snapshot["status"]["stage"], "complete")

    def test_zip_input_workflow(self) -> None:
        import zipfile

        archive = os.path.join(self.tmp, "p.zip")
        with zipfile.ZipFile(archive, "w") as zf:
            for uid, data in self.expected.items():
                zf.writestr(uid, data)
        summary = self.controller.build(archive)
        self.assertEqual(summary.units, len(self.expected))
        for uid, data in self.expected.items():
            self.assertEqual(self.controller.materialize(uid)["size"], len(data))


class TestBrowse(AppFixture):
    def setUp(self) -> None:
        super().setUp()
        self.controller.build(self.project)

    def test_units_are_listed(self) -> None:
        page = self.controller.units()
        self.assertEqual(page["total"], len(self.expected))
        self.assertEqual(len(page["units"]), len(self.expected))

    def test_units_can_be_filtered(self) -> None:
        page = self.controller.units(query="service")
        self.assertTrue(page["total"] >= 1)
        self.assertTrue(all("service" in row["uid"] for row in page["units"]))

    def test_units_paginate(self) -> None:
        first = self.controller.units(offset=0, limit=3)
        self.assertEqual(len(first["units"]), 3)
        second = self.controller.units(offset=3, limit=3)
        self.assertNotEqual(
            [u["uid"] for u in first["units"]], [u["uid"] for u in second["units"]]
        )

    def test_groups_reference_real_bases(self) -> None:
        groups = self.controller.groups()
        self.assertTrue(groups)
        for group in groups:
            self.assertIn(group["base_uid"], self.expected)
            self.assertGreaterEqual(group["member_count"], 1)

    def test_unit_detail(self) -> None:
        uid = sorted(self.expected)[0]
        detail = self.controller.unit_detail(uid)
        self.assertEqual(detail["uid"], uid)
        self.assertEqual(detail["size"], len(self.expected[uid]))
        self.assertEqual(detail["copied_bytes"] + detail["added_bytes"], detail["size"])


class TestSelectiveAndFull(AppFixture):
    def setUp(self) -> None:
        super().setUp()
        self.controller.build(self.project)

    def test_selective_read_matches_full_materialization(self) -> None:
        # The load-bearing product guarantee, asserted through the application.
        for uid, data in self.expected.items():
            for offset, length in ((0, 128), (1000, 96), (100_000, 512), (len(data) - 10, 50)):
                with self.subTest(uid=uid, offset=offset, length=length):
                    result = self.controller.read_range(uid, offset, length)
                    expected_slice = data[offset : offset + length]
                    self.assertEqual(
                        bytes.fromhex(result["preview"]["hex"])[: len(expected_slice)],
                        expected_slice[: result["preview"]["preview_bytes"]],
                    )
                    self.assertEqual(
                        result["metrics"]["bytes_returned"], len(expected_slice)
                    )

    def test_full_materialization_is_exact(self) -> None:
        for uid, data in self.expected.items():
            result = self.controller.materialize(uid)
            self.assertEqual(result["size"], len(data))
            self.assertEqual(result["metrics"]["verification"], "digest_checked")

    def test_selective_read_touches_less_than_the_unit(self) -> None:
        # A unit whose change program has several instructions. An exact
        # duplicate is represented by a single copy instruction, and against that
        # the instruction assertion below could not fail, so it would not be
        # testing anything.
        rows = [r for r in self.controller.units()["units"] if r["instructions"] > 1]
        self.assertTrue(rows, "fixture should produce multi-instruction units")
        uid = rows[0]["uid"]

        result = self.controller.read_range(uid, 100_000, 256)
        metrics = result["metrics"]
        self.assertEqual(result["mode"], "selective")
        self.assertEqual(metrics["verification"], "slice_unverified")
        self.assertLess(metrics["instructions_visited"], metrics["instructions_total"])
        full = self.controller.materialize(uid)
        self.assertLess(metrics["bytes_touched"], full["metrics"]["bytes_touched"])
        self.assertEqual(full["metrics"]["bytes_touched"], rows[0]["size"])

    def test_modes_are_distinguishable(self) -> None:
        uid = sorted(self.expected)[0]
        self.assertEqual(self.controller.read_range(uid, 0, 10)["mode"], "selective")
        self.assertEqual(self.controller.materialize(uid)["mode"], "full")

    def test_oversize_selective_read_is_refused(self) -> None:
        from ccp.ui.controller import MAX_SELECTIVE_READ_BYTES

        uid = sorted(self.expected)[0]
        with self.assertRaises(ResourceError):
            self.controller.read_range(uid, 0, MAX_SELECTIVE_READ_BYTES + 1)


class TestDeterminism(AppFixture):
    def setUp(self) -> None:
        super().setUp()
        self.controller.build(self.project)

    def test_repeated_reads_are_identical(self) -> None:
        uid = sorted(self.expected)[1]
        first = self.controller.read_range(uid, 700, 333)
        for _ in range(5):
            again = self.controller.read_range(uid, 700, 333)
            self.assertEqual(again["preview"]["hex"], first["preview"]["hex"])
            self.assertEqual(
                again["metrics"]["bytes_touched"], first["metrics"]["bytes_touched"]
            )

    def test_result_independent_of_request_order(self) -> None:
        uid = sorted(self.expected)[2]
        expected = self.controller.read_range(uid, 900, 256)["preview"]["hex"]
        for other in self.controller.units()["units"]:
            self.controller.read_range(other["uid"], 0, 64)
        self.controller.materialize(uid)
        self.assertEqual(self.controller.read_range(uid, 900, 256)["preview"]["hex"], expected)

    def test_rebuild_produces_the_same_representation(self) -> None:
        first = self.controller.snapshot()["artifact"]["artifact_bytes"]
        other = AppController(
            recents=RecentArtifacts(os.path.join(self.tmp, "appdata2")),
            output_dir=os.path.join(self.tmp, "out2"),
        )
        try:
            self.assertEqual(other.build(self.project).artifact_bytes, first)
        finally:
            other.close()


class TestVerification(AppFixture):
    def test_verify_reports_success(self) -> None:
        self.controller.build(self.project)
        report = self.controller.verify()
        self.assertTrue(report["ok"])
        self.assertEqual(report["units_ok"], len(self.expected))
        self.assertEqual(report["failures"], [])

    def test_damaged_artifact_fails_verification(self) -> None:
        self.controller.build(self.project)
        saved = self.controller.save_artifact(os.path.join(self.tmp, "a.ccp"))
        with open(saved, "r+b") as handle:
            handle.seek(os.path.getsize(saved) - 40)
            handle.write(b"\xff" * 16)
        self.controller.close()
        fresh = AppController(
            recents=RecentArtifacts(os.path.join(self.tmp, "appdata3")),
            output_dir=os.path.join(self.tmp, "out3"),
        )
        try:
            with self.assertRaises((VerificationError, MalformedArtifactError)):
                fresh.open_artifact(saved)
                fresh.verify()
        finally:
            fresh.close()


class TestLifecycle(AppFixture):
    def test_open_inspect_read_verify_close(self) -> None:
        self.controller.build(self.project)
        path = self.controller.save_artifact(os.path.join(self.tmp, "a.ccp"))
        self.controller.close()
        self.assertIs(self.controller.state, AppState.CLOSED)

        summary = self.controller.open_artifact(path)
        self.assertEqual(summary.units, len(self.expected))
        self.assertTrue(self.controller.verify()["ok"])
        uid = sorted(self.expected)[0]
        self.assertEqual(
            self.controller.materialize(uid)["size"], len(self.expected[uid])
        )
        self.controller.close()

    def test_repeated_close_is_idempotent(self) -> None:
        self.controller.build(self.project)
        self.controller.close()
        self.controller.close()
        self.assertIs(self.controller.state, AppState.CLOSED)

    def test_operations_without_artifact_raise(self) -> None:
        with self.assertRaises(ResourceError):
            self.controller.units()
        with self.assertRaises(ResourceError):
            self.controller.verify()
        with self.assertRaises(ResourceError):
            self.controller.materialize("anything")

    def test_building_again_replaces_and_closes_the_previous_artifact(self) -> None:
        self.controller.build(self.project)
        self.controller.build(self.project)
        self.assertIs(self.controller.state, AppState.READY)
        self.assertEqual(self.controller.units()["total"], len(self.expected))

    def test_application_stays_usable_after_a_recoverable_error(self) -> None:
        self.controller.build(self.project)
        with self.assertRaises(UnitNotFoundError):
            self.controller.materialize("no-such-unit")
        # Still ready, still serving: one bad request must not cost the build.
        self.assertIs(self.controller.state, AppState.READY)
        uid = sorted(self.expected)[0]
        self.assertEqual(self.controller.read_range(uid, 0, 32)["metrics"]["bytes_returned"], 32)


class TestErrors(AppFixture):
    def test_every_product_error_class_is_described(self) -> None:
        cases = [
            (InputError("bad input"), "input"),
            (MalformedArtifactError("bad container"), "malformed"),
            (UnitNotFoundError("x"), "unit"),
            (InvalidRangeError("bad range"), "range"),
            (VerificationError("mismatch"), "verification"),
            (UnsupportedOperationError("mutate"), "unsupported"),
            (ResourceError("closed"), "resource"),
            (ProductError("generic"), "product"),
            (FileNotFoundError("nope"), "filesystem"),
            (PermissionError("denied"), "filesystem"),
            (MemoryError(), "resource"),
            (RuntimeError("boom"), "internal"),
        ]
        for error, kind in cases:
            with self.subTest(error=type(error).__name__):
                described = describe_error(error)
                self.assertEqual(described.kind, kind)
                for field in (described.title, described.what, described.why, described.next_step):
                    self.assertTrue(field.strip(), "every field must say something")

    def test_internal_failure_is_not_hidden(self) -> None:
        described = describe_error(RuntimeError("kaboom"))
        self.assertIn("kaboom", described.what)

    def test_bad_input_path(self) -> None:
        with self.assertRaises(InputError):
            self.controller.build(os.path.join(self.tmp, "nothing-here"))
        self.assertIsNotNone(self.controller.snapshot()["error"])

    def test_bad_range(self) -> None:
        self.controller.build(self.project)
        with self.assertRaises(InvalidRangeError):
            self.controller.read_range(sorted(self.expected)[0], -1, 10)

    def test_malformed_artifact(self) -> None:
        path = os.path.join(self.tmp, "junk.ccp")
        with open(path, "wb") as handle:
            handle.write(b"definitely not a ccp container")
        with self.assertRaises(MalformedArtifactError):
            self.controller.open_artifact(path)


class TestExport(AppFixture):
    def setUp(self) -> None:
        super().setUp()
        self.controller.build(self.project)

    def test_save_artifact_writes_a_reopenable_file(self) -> None:
        path = self.controller.save_artifact(os.path.join(self.tmp, "saved.ccp"))
        self.assertTrue(os.path.isfile(path))
        other = AppController(
            recents=RecentArtifacts(os.path.join(self.tmp, "d2")),
            output_dir=os.path.join(self.tmp, "o2"),
        )
        try:
            self.assertEqual(other.open_artifact(path).units, len(self.expected))
        finally:
            other.close()

    def test_save_refuses_to_clobber_without_permission(self) -> None:
        from ccp.integration.errors import ExportError

        path = os.path.join(self.tmp, "once.ccp")
        self.controller.save_artifact(path)
        with self.assertRaises(ExportError):
            self.controller.save_artifact(path)
        self.controller.save_artifact(path, overwrite=True)

    def test_export_unit_writes_exact_bytes(self) -> None:
        uid = sorted(self.expected)[0]
        destination = os.path.join(self.tmp, "exported")
        result = self.controller.export_unit(uid, destination)
        with open(result["path"], "rb") as handle:
            self.assertEqual(handle.read(), self.expected[uid])
        self.assertTrue(result["digest_checked"])

    def test_export_cannot_escape_the_destination(self) -> None:
        from ccp.integration.errors import ExportError

        uid = sorted(self.expected)[0]
        with self.assertRaises(ExportError):
            self.controller.export_unit(uid, os.path.join(self.tmp, "safe"), name="../evil")

    def test_recents_records_saved_artifacts(self) -> None:
        self.controller.save_artifact(os.path.join(self.tmp, "r.ccp"))
        self.assertEqual(len(self.controller.recents()), 1)


class TestResourceBounds(AppFixture):
    def test_input_over_the_limit_is_refused(self) -> None:
        tight = AppController(
            limits=InputLimits(max_units=1),
            recents=RecentArtifacts(os.path.join(self.tmp, "d3")),
            output_dir=os.path.join(self.tmp, "o3"),
        )
        try:
            with self.assertRaises(InputError):
                tight.build(self.project)
        finally:
            tight.close()

    def test_no_shared_state_between_controllers(self) -> None:
        other = AppController(
            recents=RecentArtifacts(os.path.join(self.tmp, "d4")),
            output_dir=os.path.join(self.tmp, "o4"),
        )
        try:
            self.controller.build(self.project)
            self.assertIs(other.state, AppState.NO_INPUT)
        finally:
            other.close()


class TestHTTPSurface(AppFixture):
    """The application driven over a real socket, as the browser drives it."""

    def setUp(self) -> None:
        super().setUp()
        self.server = AppServer(controller=self.controller)
        self.server.start()
        self.base = f"http://127.0.0.1:{self.server.port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self._tmp.cleanup()

    def call(self, path: str, data=None, method=None):
        separator = "&" if "?" in path else "?"
        url = f"{self.base}{path}{separator}t={self.server.token}"
        body = json.dumps(data).encode() if data is not None else None
        request = urllib.request.Request(
            url, data=body, method=method or ("POST" if data is not None else "GET")
        )
        if body:
            request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())

    def test_index_is_served(self) -> None:
        with urllib.request.urlopen(f"{self.base}/?t={self.server.token}") as response:
            self.assertEqual(response.status, 200)
            self.assertIn(b"CCP Forge", response.read())

    def test_requests_without_a_token_are_refused(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(f"{self.base}/api/state")
        self.assertEqual(ctx.exception.code, 403)

    def test_requests_with_a_wrong_token_are_refused(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(f"{self.base}/api/state?t=not-the-token")
        self.assertEqual(ctx.exception.code, 403)

    def test_full_workflow_over_http(self) -> None:
        status, _ = self.call("/api/build", {"path": self.project})
        self.assertEqual(status, 202)
        self.assertTrue(self.controller.wait_for_build(120))

        state = self.call("/api/state")[1]
        self.assertEqual(state["state"], "ready")
        self.assertEqual(state["artifact"]["units"], len(self.expected))
        self.assertTrue(state["artifact"]["verified"])

        units = self.call("/api/units?limit=100")[1]
        self.assertEqual(units["total"], len(self.expected))
        uid = units["units"][0]["uid"]

        self.assertTrue(self.call("/api/groups")[1]["groups"])
        self.assertEqual(self.call(f"/api/unit?uid={uid}")[1]["uid"], uid)

        selective = self.call("/api/read", {"uid": uid, "offset": 100, "length": 128})[1]
        self.assertEqual(selective["mode"], "selective")
        self.assertEqual(selective["metrics"]["bytes_returned"], 128)

        full = self.call("/api/materialize", {"uid": uid})[1]
        self.assertEqual(full["mode"], "full")
        self.assertEqual(full["metrics"]["verification"], "digest_checked")

        self.assertTrue(self.call("/api/verify", {})[1]["ok"])
        saved = self.call(
            "/api/save", {"path": os.path.join(self.tmp, "http.ccp"), "overwrite": True}
        )[1]
        self.assertTrue(os.path.isfile(saved["path"]))

        exported = self.call(
            "/api/export",
            {"uid": uid, "dir": os.path.join(self.tmp, "http-exp"), "overwrite": True},
        )[1]
        with open(exported["path"], "rb") as handle:
            self.assertEqual(handle.read(), self.expected[uid])

        self.assertTrue(self.call("/api/close", {})[1]["closed"])

    def test_errors_come_back_as_user_facing_json(self) -> None:
        self.call("/api/build", {"path": self.project})
        self.controller.wait_for_build(120)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.call("/api/read", {"uid": "missing", "offset": 0, "length": 8})
        payload = json.loads(ctx.exception.read())
        self.assertEqual(ctx.exception.code, 400)
        for key in ("title", "what", "why", "next", "kind"):
            self.assertIn(key, payload["error"])
        self.assertNotIn("Traceback", payload["error"]["what"])

    def test_unknown_endpoint_is_a_clean_404(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.call("/api/nope")
        self.assertEqual(ctx.exception.code, 404)

    def test_upload_rejects_a_non_zip(self) -> None:
        request = urllib.request.Request(
            f"{self.base}/api/upload?name=evil.txt&t={self.server.token}",
            data=b"hello",
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request)
        self.assertEqual(ctx.exception.code, 400)

    def test_upload_sanitises_the_filename(self) -> None:
        import zipfile

        archive = os.path.join(self.tmp, "u.zip")
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("a.txt", b"x" * 100)
        with open(archive, "rb") as handle:
            payload = handle.read()
        request = urllib.request.Request(
            f"{self.base}/api/upload?name=../../escape.zip&t={self.server.token}",
            data=payload,
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            result = json.loads(response.read())
        self.assertEqual(
            os.path.dirname(os.path.realpath(result["path"])),
            os.path.realpath(self.server.upload_dir),
        )


if __name__ == "__main__":
    unittest.main()
