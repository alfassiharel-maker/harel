"""Tests for the distribution bundler.

    cd bitengine && python3 -m unittest discover -s tests -t .

The bundler's one real risk is shipping something that does not run, so the
build is executed rather than inspected. The other risk is the .pyz quietly
acquiring a third-party import, which would break the "needs nothing installed"
promise on a user's machine and not on ours.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bundle  # noqa: E402


class SandboxedBuild:
    """Point the bundler's output at a temporary directory.

    `bundle.clean()` deletes `dist/` and `build/` outright. Run against the
    module's defaults, the suite would delete whatever the developer had just
    built — so the paths are redirected for the duration of the test and
    restored afterwards.
    """

    def __enter__(self) -> SandboxedBuild:
        self._directory = tempfile.TemporaryDirectory()
        self._saved = (bundle.DIST, bundle.BUILD)
        bundle.DIST = os.path.join(self._directory.name, "dist")
        bundle.BUILD = os.path.join(self._directory.name, "build")
        return self

    def __exit__(self, *exc_info: object) -> None:
        bundle.DIST, bundle.BUILD = self._saved
        self._directory.cleanup()


class Pyz(unittest.TestCase):
    """Builds the real archive once; the build is a few hundred milliseconds."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.sandbox = SandboxedBuild().__enter__()
        cls.archive = bundle.build_pyz()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.sandbox.__exit__()

    def test_archive_exists_and_is_small(self) -> None:
        self.assertTrue(os.path.exists(self.archive))
        # The engine is standard-library only, so the archive is tens of KB. A
        # sudden jump means something heavy was pulled in.
        self.assertLess(os.path.getsize(self.archive), 1024 * 1024)

    def test_contains_the_engine_and_not_the_ui(self) -> None:
        with zipfile.ZipFile(self.archive) as archive:
            names = {os.path.basename(n) for n in archive.namelist()}
        for module in ("l1.py", "l2.py", "l3.py", "cli.py", "__main__.py"):
            self.assertIn(module, names)
        # app.py imports Streamlit. Bundling it would break the promise that the
        # .pyz runs with nothing installed.
        self.assertNotIn("app.py", names)
        self.assertNotIn("webui.py", names)

    def test_it_actually_runs(self) -> None:
        result = subprocess.run(
            [sys.executable, self.archive, "goals"], capture_output=True, text=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("video-frame-delta", result.stdout)

    def test_round_trips_a_real_file(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "source.bin")
            container = os.path.join(directory, "out.bite")
            restored = os.path.join(directory, "restored.bin")
            payload = bytes(range(256)) * 400
            with open(source, "wb") as handle:
                handle.write(payload)

            for argv in (
                [source, container, "--goal", "anchor-dedup", "--block-size", "4KB"],
                None,
            ):
                if argv is None:
                    command = [sys.executable, self.archive, "unpack", container, restored]
                else:
                    command = [sys.executable, self.archive, "pack", *argv]
                result = subprocess.run(command, capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("PASS", result.stdout)

            with open(restored, "rb") as handle:
                self.assertEqual(handle.read(), payload)

    def test_launchers_written(self) -> None:
        for name in ("bitengine.sh", "bitengine.bat"):
            path = os.path.join(bundle.DIST, name)
            self.assertTrue(os.path.exists(path), name)
        self.assertTrue(os.access(os.path.join(bundle.DIST, "bitengine.sh"), os.X_OK))

    def test_verify_rejects_a_broken_archive(self) -> None:
        with self.assertRaises(SystemExit):
            bundle.verify_pyz(os.path.join(bundle.DIST, "does-not-exist.pyz"))


class Ui(unittest.TestCase):
    def test_staging_includes_the_ui_and_its_requirements(self) -> None:
        with SandboxedBuild():
            directory = bundle.stage_ui()
            for name in ("app.py", "webui.py", "l1.py", "requirements.txt", "run-ui.sh", "run-ui.bat"):
                self.assertTrue(os.path.exists(os.path.join(directory, name)), name)
            with open(os.path.join(directory, "requirements.txt"), encoding="utf-8") as handle:
                self.assertIn("streamlit", handle.read())


class DoesNotTouchTheRealDist(unittest.TestCase):
    """The suite must not delete artefacts the developer just built.

    This is a regression test for exactly that: `make bundle-bitengine` followed
    by the test suite used to leave no `dist/` behind.
    """

    def test_build_outputs_survive_the_suite(self) -> None:
        marker = os.path.join(bundle.DIST, "marker.txt")
        os.makedirs(bundle.DIST, exist_ok=True)
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write("built by the developer")
        try:
            with SandboxedBuild():
                bundle.build_pyz()
                bundle.stage_ui()
            self.assertTrue(os.path.exists(marker), "the suite deleted the real dist/")
        finally:
            if os.path.exists(marker):
                os.remove(marker)


if __name__ == "__main__":
    unittest.main()
