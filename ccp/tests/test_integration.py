"""Tests for the integration layer: input adapters, pipeline, workspace.

    python3 -m unittest discover -s ccp/tests -t .

The zip tests are mostly hostile-input tests. A zip is the product's headline
input and it is completely untrusted, so the cases that matter are the malicious
ones: traversal names, absolute paths, symlinks and expansion bombs.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
import zipfile

from ccp.integration import (
    BuildPipeline,
    CancellationToken,
    InputError,
    InputLimits,
    ProjectDirectorySource,
    RecentArtifacts,
    RecentEntry,
    Stage,
    ZipProjectSource,
    analyse_input,
    atomic_write,
    safe_export_path,
    source_for,
)
from ccp.integration.errors import CancelledError, ExportError


def deterministic_bytes(size: int, seed: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < size:
        out += hashlib.blake2b(
            counter.to_bytes(8, "little") + seed.to_bytes(8, "little"), digest_size=64
        ).digest()
        counter += 1
    return bytes(out[:size])


def make_project(root: str, versions: int = 4, size: int = 20_000) -> dict:
    """A project with several near-identical revisions — what CCP is for."""
    body = deterministic_bytes(size, seed=5)
    expected = {}
    for index in range(versions):
        for name in ("app.py", "util.py"):
            relative = f"v{index}/{name}"
            path = os.path.join(root, f"v{index}", name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            patch = deterministic_bytes(64, seed=100 + index)
            data = body[:500] + patch + body[564:]
            with open(path, "wb") as handle:
                handle.write(data)
            expected[relative] = data
    return expected


class TestDirectoryInput(unittest.TestCase):
    def test_analyse_counts_files_and_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            expected = make_project(tmp)
            analysis = analyse_input(tmp)
            self.assertEqual(analysis.kind, "directory")
            self.assertEqual(analysis.unit_count, len(expected))
            self.assertEqual(analysis.total_bytes, sum(len(v) for v in expected.values()))

    def test_version_control_directories_are_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            make_project(tmp, versions=1)
            git = os.path.join(tmp, ".git", "objects")
            os.makedirs(git)
            with open(os.path.join(git, "junk"), "wb") as handle:
                handle.write(b"x" * 100)
            uids = [u.uid for u in ProjectDirectorySource(tmp).units()]
            self.assertTrue(uids)
            self.assertFalse(any(u.startswith(".git/") for u in uids))
            self.assertEqual(analyse_input(tmp).unit_count, len(uids))

    def test_empty_directory_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(InputError):
                analyse_input(tmp)

    def test_missing_path_is_refused(self) -> None:
        with self.assertRaises(InputError):
            analyse_input("/no/such/place/at/all")

    def test_limits_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            make_project(tmp, versions=2)
            with self.assertRaises(InputError):
                analyse_input(tmp, InputLimits(max_units=1))
            with self.assertRaises(InputError):
                analyse_input(tmp, InputLimits(max_total_bytes=100))
            with self.assertRaises(InputError):
                analyse_input(tmp, InputLimits(max_unit_bytes=100))


class TestZipInput(unittest.TestCase):
    def _zip(self, tmp: str, entries: dict, name: str = "p.zip") -> str:
        path = os.path.join(tmp, name)
        with zipfile.ZipFile(path, "w") as archive:
            for entry, data in entries.items():
                archive.writestr(entry, data)
        return path

    def test_reads_entries_as_units(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            entries = {"a/one.txt": b"hello" * 100, "a/two.txt": b"hella" * 100}
            path = self._zip(tmp, entries)
            analysis = analyse_input(path)
            self.assertEqual(analysis.kind, "zip")
            self.assertEqual(analysis.unit_count, 2)
            got = {u.uid: u.data for u in ZipProjectSource(path).units()}
            self.assertEqual(got, entries)

    def test_path_traversal_entry_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._zip(tmp, {"../escape.txt": b"bad", "ok.txt": b"good"})
            uids = [u.uid for u in ZipProjectSource(path).units()]
            self.assertEqual(uids, ["ok.txt"])
            self.assertTrue(
                any("traversal" in reason for _, reason in analyse_input(path).skipped)
            )

    def test_absolute_entry_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._zip(tmp, {"/etc/passwd": b"bad", "ok.txt": b"good"})
            self.assertEqual([u.uid for u in ZipProjectSource(path).units()], ["ok.txt"])

    def test_symlink_entry_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s.zip")
            with zipfile.ZipFile(path, "w") as archive:
                info = zipfile.ZipInfo("link")
                info.external_attr = (0xA1FF) << 16  # S_IFLNK | 0777
                archive.writestr(info, "/etc/passwd")
                archive.writestr("ok.txt", b"good")
            self.assertEqual([u.uid for u in ZipProjectSource(path).units()], ["ok.txt"])

    def test_zip_bomb_ratio_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bomb.zip")
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
                # Highly compressible: a huge run of one byte.
                archive.writestr("big", b"\0" * (8 << 20))
            with self.assertRaises(InputError) as ctx:
                analyse_input(path)
            self.assertIn("expands", str(ctx.exception))

    def test_not_a_zip_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "plain.bin")
            with open(path, "wb") as handle:
                handle.write(b"not a zip")
            with self.assertRaises(InputError):
                analyse_input(path)

    def test_corrupt_zip_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            good = self._zip(tmp, {"a.txt": b"x" * 500})
            with open(good, "r+b") as handle:
                handle.seek(20)
                handle.write(b"\xff" * 40)
            with self.assertRaises(InputError):
                list(source_for(good).units())


class TestPipeline(unittest.TestCase):
    def test_build_produces_a_verified_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            expected = make_project(tmp)
            outcome = BuildPipeline().run(tmp)
            try:
                self.assertEqual(outcome.units_verified, len(expected))
                self.assertTrue(outcome.artifact.is_verified)
                for uid, data in expected.items():
                    self.assertEqual(outcome.artifact.materialize(uid).data, data)
            finally:
                outcome.artifact.close()

    def test_progress_reaches_every_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            make_project(tmp)
            seen = []
            outcome = BuildPipeline().run(tmp, on_progress=lambda p: seen.append(p.stage))
            outcome.artifact.close()
            for stage in (Stage.ANALYSING, Stage.READING, Stage.BUILDING,
                          Stage.VERIFYING, Stage.COMPLETE):
                self.assertIn(stage, seen)

    def test_cancellation_before_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            make_project(tmp)
            token = CancellationToken()
            token.cancel()
            with self.assertRaises(CancelledError):
                BuildPipeline().run(tmp, cancel=token)

    def test_cancellation_during_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            make_project(tmp, versions=8)
            token = CancellationToken()

            def on_progress(progress) -> None:
                if progress.stage is Stage.READING:
                    token.cancel()

            with self.assertRaises(CancelledError):
                BuildPipeline().run(tmp, on_progress=on_progress, cancel=token)

    def test_zip_input_builds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body = deterministic_bytes(9000, 3)
            entries = {f"v{i}/f.bin": body[:400] + bytes([i]) + body[401:] for i in range(4)}
            path = os.path.join(tmp, "p.zip")
            with zipfile.ZipFile(path, "w") as archive:
                for name, data in entries.items():
                    archive.writestr(name, data)
            outcome = BuildPipeline().run(path)
            try:
                self.assertEqual(outcome.units_verified, len(entries))
                for uid, data in entries.items():
                    self.assertEqual(outcome.artifact.materialize(uid).data, data)
            finally:
                outcome.artifact.close()


class TestWorkspace(unittest.TestCase):
    def test_atomic_write_creates_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "sub", "a.bin")
            atomic_write(target, b"hello")
            with open(target, "rb") as handle:
                self.assertEqual(handle.read(), b"hello")

    def test_atomic_write_refuses_to_clobber(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "a.bin")
            atomic_write(target, b"first")
            with self.assertRaises(ExportError):
                atomic_write(target, b"second")
            with open(target, "rb") as handle:
                self.assertEqual(handle.read(), b"first")
            atomic_write(target, b"second", overwrite=True)
            with open(target, "rb") as handle:
                self.assertEqual(handle.read(), b"second")

    def test_atomic_write_leaves_no_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            atomic_write(os.path.join(tmp, "a.bin"), b"x")
            self.assertEqual(sorted(os.listdir(tmp)), ["a.bin"])

    def test_safe_export_path_allows_normal_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            resolved = safe_export_path(tmp, "sub/dir/file.txt")
            self.assertTrue(resolved.startswith(os.path.realpath(tmp) + os.sep))

    def test_safe_export_path_rejects_escapes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for bad in ("../out.txt", "a/../../out.txt", "/etc/passwd", "C:/win.txt", "", ".."):
                with self.subTest(name=bad):
                    with self.assertRaises(ExportError):
                        safe_export_path(tmp, bad)

    def test_recents_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_path = os.path.join(tmp, "a.ccp")
            atomic_write(artifact_path, b"x")
            recents = RecentArtifacts(os.path.join(tmp, "data"))
            self.assertEqual(recents.load(), [])
            recents.remember(
                RecentEntry(artifact_path, "label", 3, 100, 50)
            )
            loaded = recents.load()
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].path, artifact_path)

    def test_recents_drops_missing_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recents = RecentArtifacts(os.path.join(tmp, "data"))
            recents.remember(RecentEntry(os.path.join(tmp, "gone.ccp"), "x", 1, 1, 1))
            self.assertEqual(recents.load(), [])

    def test_corrupt_recents_file_is_survivable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = os.path.join(tmp, "data")
            os.makedirs(data_dir)
            recents = RecentArtifacts(data_dir)
            with open(recents.path, "w", encoding="utf-8") as handle:
                handle.write("{ not json")
            self.assertEqual(recents.load(), [])


if __name__ == "__main__":
    unittest.main()
