"""Bundle BitEngine for distribution.

    python3 bundle.py                 CLI as a single .pyz, plus launchers
    python3 bundle.py --with-ui       also stage the dashboard
    python3 bundle.py --onefile       also build a native binary (needs PyInstaller)

Three tiers, because "standalone" means different things and only the third
genuinely removes the Python requirement.

**dist/bitengine.pyz** — the CLI as one file, built with the standard library's
`zipapp`. Runs on Windows, macOS and Linux with `python3 bitengine.pyz`, needs
nothing installed, and is a few tens of kilobytes because the engine imports
only the standard library. This is the tier that matches how the engine was
built and the one to prefer.

**dist/ui/** — the dashboard staged with launcher scripts. Streamlit is a real
dependency, so this tier needs `pip install -r requirements.txt` once. It cannot
be a single file and pretending otherwise would just move the failure later.

**dist/bitengine (or .exe)** — a native executable via PyInstaller, for users
with no Python at all. PyInstaller is third-party and platform-specific: it
produces a binary for the machine it runs on and cannot cross-compile, so a
Windows .exe has to be built on Windows. Skipped with a clear message when
PyInstaller is absent rather than failing the whole bundle.
"""

from __future__ import annotations

import argparse
import compileall
import os
import shutil
import subprocess  # noqa: S404 — invokes PyInstaller, and only on request
import sys
import zipapp
from collections.abc import Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(HERE, "dist")
BUILD = os.path.join(HERE, "build")

# The engine and the CLI. Deliberately excludes app.py and webui.py: the .pyz
# must stay importable with the standard library alone, and bundling a module
# that imports Streamlit would break that promise the first time someone ran it.
CLI_MODULES = ("l1.py", "l2.py", "l3.py", "cli.py")
UI_MODULES = ("l1.py", "l2.py", "l3.py", "cli.py", "webui.py", "app.py")

LAUNCHERS = {
    "bitengine.sh": """#!/bin/sh
# BitEngine CLI. Requires Python 3.11 or newer on PATH.
exec python3 "$(dirname "$0")/bitengine.pyz" "$@"
""",
    "bitengine.bat": """@echo off
REM BitEngine CLI. Requires Python 3.11 or newer on PATH.
python "%~dp0bitengine.pyz" %*
""",
}

UI_LAUNCHERS = {
    "run-ui.sh": """#!/bin/sh
# BitEngine dashboard. First run: pip install -r requirements.txt
cd "$(dirname "$0")" || exit 1
exec python3 -m streamlit run app.py
""",
    "run-ui.bat": """@echo off
REM BitEngine dashboard. First run: pip install -r requirements.txt
cd /d "%~dp0"
python -m streamlit run app.py
""",
}

REQUIREMENTS = """# The dashboard only. The engine and the CLI need nothing.
streamlit>=1.30
pandas>=2.0

# Optional. Enables the `zstd --patch-from` comparison panel, which is the
# same-information rival BitEngine is measured against in reports/.
zstandard>=0.22
"""


def clean() -> None:
    for directory in (DIST, BUILD):
        if os.path.isdir(directory):
            shutil.rmtree(directory)


def _write(path: str, text: str, executable: bool = False) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    if executable:
        os.chmod(path, 0o755)  # noqa: S103 — a launcher script must be runnable


def build_pyz(target_python: str = "/usr/bin/env python3") -> str:
    """The CLI as one executable archive, using only the standard library."""
    staging = os.path.join(BUILD, "cli")
    os.makedirs(staging, exist_ok=True)
    for name in CLI_MODULES:
        shutil.copy2(os.path.join(HERE, name), os.path.join(staging, name))

    # `cli.main` already returns an exit code, so the entry point only has to
    # hand it to the interpreter.
    _write(
        os.path.join(staging, "__main__.py"),
        "import sys\n\nimport cli\n\nsys.exit(cli.main())\n",
    )

    # Compiling here turns a syntax error into a build failure rather than
    # something the user discovers on first run.
    if not compileall.compile_dir(staging, quiet=2, force=True):
        raise SystemExit("bundle: a staged module failed to compile")

    os.makedirs(DIST, exist_ok=True)
    output = os.path.join(DIST, "bitengine.pyz")
    zipapp.create_archive(staging, output, interpreter=target_python, compressed=True)
    os.chmod(output, 0o755)  # noqa: S103 — the archive is meant to be executed

    for name, body in LAUNCHERS.items():
        _write(os.path.join(DIST, name), body, executable=name.endswith(".sh"))
    return output


def stage_ui() -> str:
    """The dashboard plus launchers. Needs pip install; cannot be one file."""
    ui_dir = os.path.join(DIST, "ui")
    os.makedirs(ui_dir, exist_ok=True)
    for name in UI_MODULES:
        shutil.copy2(os.path.join(HERE, name), os.path.join(ui_dir, name))
    _write(os.path.join(ui_dir, "requirements.txt"), REQUIREMENTS)
    for name, body in UI_LAUNCHERS.items():
        _write(os.path.join(ui_dir, name), body, executable=name.endswith(".sh"))
    return ui_dir


def build_onefile() -> str | None:
    """A native binary for the current platform, if PyInstaller is available."""
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        return None

    entry = os.path.join(BUILD, "entry.py")
    os.makedirs(BUILD, exist_ok=True)
    _write(entry, "import sys\n\nimport cli\n\nsys.exit(cli.main())\n")

    subprocess.run(  # noqa: S603
        [
            sys.executable, "-m", "PyInstaller",
            "--onefile", "--name", "bitengine",
            "--distpath", DIST,
            "--workpath", os.path.join(BUILD, "pyinstaller"),
            "--specpath", BUILD,
            "--paths", HERE,
            "--hidden-import", "l1", "--hidden-import", "l2", "--hidden-import", "l3",
            "--noconfirm", "--clean", "--log-level", "WARN",
            entry,
        ],
        check=True,
    )
    binary = os.path.join(DIST, "bitengine.exe" if os.name == "nt" else "bitengine")
    return binary if os.path.exists(binary) else None


def verify_pyz(path: str) -> None:
    """Run the bundle. A build that was never executed is not a build."""
    result = subprocess.run(  # noqa: S603
        [sys.executable, path, "goals"], capture_output=True, text=True, check=False
    )
    if result.returncode != 0 or "video-frame-delta" not in result.stdout:
        raise SystemExit(
            f"bundle: {os.path.basename(path)} did not run correctly\n"
            f"exit {result.returncode}\n{result.stdout}\n{result.stderr}"
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--with-ui", action="store_true", help="also stage the dashboard")
    parser.add_argument("--onefile", action="store_true", help="also build a native binary")
    parser.add_argument("--interpreter", default="/usr/bin/env python3", help="shebang for the .pyz")
    parser.add_argument("--keep-build", action="store_true", help="do not delete the staging directory")
    args = parser.parse_args(argv)

    clean()
    archive = build_pyz(args.interpreter)
    verify_pyz(archive)
    print(f"CLI        {os.path.relpath(archive, HERE)}  ({os.path.getsize(archive) / 1024:.0f} KB, verified)")
    print(f"launchers  dist/bitengine.sh, dist/bitengine.bat")

    if args.with_ui:
        ui_dir = stage_ui()
        print(f"dashboard  {os.path.relpath(ui_dir, HERE)}/  (run-ui.sh / run-ui.bat, needs pip install)")

    if args.onefile:
        binary = build_onefile()
        if binary:
            print(f"binary     {os.path.relpath(binary, HERE)}  ({os.path.getsize(binary) / 1024 / 1024:.1f} MB)")
        else:
            print("binary     skipped — PyInstaller is not installed (pip install pyinstaller)")
            print("           note: it cannot cross-compile; build the .exe on Windows")

    if not args.keep_build and os.path.isdir(BUILD):
        shutil.rmtree(BUILD)

    print("\nRun it:")
    print("  python3 dist/bitengine.pyz goals")
    print("  python3 dist/bitengine.pyz pack v2.bin out.bite --reference v1.bin")
    return 0


if __name__ == "__main__":
    sys.exit(main())
