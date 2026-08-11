#!/usr/bin/env python3
"""Build the standalone CCP Forge application.

Run this on the platform you are building for. It produces a single executable
that bundles a Python runtime and the `ccp` package, so the end user installs
nothing and needs no Python:

    python3 packaging/build_app.py

    Windows   dist/CCPForge.exe
    macOS     dist/CCPForge
    Linux     dist/CCPForge

PyInstaller builds for the platform it runs on and cannot cross-compile; a
Windows .exe must be produced by running this same command on a Windows host.
That is not a workaround, it is the architecture the product direction already
specifies -- the agent/development environment and the target build environment
are different machines, and this script is what the target machine runs.

`--zipapp` additionally produces `dist/CCPForge.pyz`, a single-file application
built with the standard library alone. It needs Python 3.9+ on the machine that
runs it, so it is not the primary deliverable, but it is useful where an
unsigned executable is awkward and it requires no build tooling at all.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import zipapp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "dist")
BUILD = os.path.join(ROOT, "build")
APP_NAME = "CCPForge"

# The launcher script PyInstaller compiles. Kept as a generated file rather than
# a checked-in one so there is exactly one definition of how the app starts:
# ccp.ui.launcher.main.
ENTRY_SOURCE = '''"""Generated entry point for the packaged CCP Forge application."""

import multiprocessing
import sys

from ccp.ui.launcher import main

if __name__ == "__main__":
    # Required before anything else on Windows when frozen, or a child process
    # re-runs the whole application instead of starting a worker.
    multiprocessing.freeze_support()
    sys.exit(main())
'''


def version() -> str:
    sys.path.insert(0, ROOT)
    import ccp  # noqa: PLC0415 - imported after the path is set

    return ccp.__version__


def clean() -> None:
    for path in (DIST, BUILD):
        if os.path.isdir(path):
            shutil.rmtree(path)
    spec = os.path.join(ROOT, f"{APP_NAME}.spec")
    if os.path.isfile(spec):
        os.unlink(spec)


def write_entry() -> str:
    os.makedirs(BUILD, exist_ok=True)
    entry = os.path.join(BUILD, "ccp_forge_main.py")
    with open(entry, "w", encoding="utf-8") as handle:
        handle.write(ENTRY_SOURCE)
    return entry


def build_executable(onefile: bool = True, console: bool = True) -> str:
    """Build the native executable with PyInstaller."""
    try:
        import PyInstaller  # noqa: F401,PLC0415
    except ImportError:
        raise SystemExit(
            "PyInstaller is not installed. Install the build requirements:\n"
            "    python3 -m pip install -r packaging/requirements-build.txt"
        )

    entry = write_entry()
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--name",
        APP_NAME,
        "--distpath",
        DIST,
        "--workpath",
        os.path.join(BUILD, "pyinstaller"),
        "--specpath",
        BUILD,
        "--noconfirm",
        "--clean",
        # The UI is one HTML string inside ccp.ui.web, so there is no data file
        # to locate at runtime and nothing to go missing on another machine.
        "--hidden-import",
        "ccp.ui.web",
        "--exclude-module",
        "tkinter",
        "--exclude-module",
        "unittest",
        "--exclude-module",
        "pydoc",
    ]
    command.append("--onefile" if onefile else "--onedir")
    # A console window is deliberate: the application prints its URL there, and
    # closing it is how a user quits. A windowed build would leave a server
    # running with no visible way to stop it.
    command.append("--console" if console else "--windowed")

    icon = os.path.join(ROOT, "packaging", "icon.ico")
    if os.path.isfile(icon):
        command += ["--icon", icon]

    version_file = _write_windows_version_info()
    if version_file:
        command += ["--version-file", version_file]

    command.append(entry)
    print("running:", " ".join(command))
    subprocess.run(command, check=True, cwd=ROOT)

    produced = os.path.join(DIST, APP_NAME + (".exe" if os.name == "nt" else ""))
    if not os.path.exists(produced):
        raise SystemExit(f"expected {produced} to exist after the build")
    return produced


def _write_windows_version_info() -> str:
    """Windows executable metadata. Ignored on other platforms."""
    if os.name != "nt":
        return ""
    numbers = version().split(".")
    while len(numbers) < 4:
        numbers.append("0")
    quad = ", ".join(numbers[:4])
    path = os.path.join(BUILD, "version_info.txt")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(
            f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({quad}), prodvers=({quad}), mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'CCP'),
      StringStruct('FileDescription', 'CCP Forge'),
      StringStruct('FileVersion', '{version()}'),
      StringStruct('InternalName', '{APP_NAME}'),
      StringStruct('OriginalFilename', '{APP_NAME}.exe'),
      StringStruct('ProductName', 'CCP Forge'),
      StringStruct('ProductVersion', '{version()}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
        )
    return path


def build_zipapp() -> str:
    """A single-file .pyz using only the standard library."""
    staging = os.path.join(BUILD, "zipapp")
    if os.path.isdir(staging):
        shutil.rmtree(staging)
    os.makedirs(staging)

    shutil.copytree(
        os.path.join(ROOT, "ccp"),
        os.path.join(staging, "ccp"),
        ignore=shutil.ignore_patterns(
            "tests", "benchmarks", "__pycache__", "*.pyc", "docs"
        ),
    )
    with open(os.path.join(staging, "__main__.py"), "w", encoding="utf-8") as handle:
        handle.write(ENTRY_SOURCE)

    os.makedirs(DIST, exist_ok=True)
    target = os.path.join(DIST, f"{APP_NAME}.pyz")
    zipapp.create_archive(staging, target, interpreter="/usr/bin/env python3")
    return target


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build the CCP Forge application")
    parser.add_argument("--zipapp", action="store_true", help="also build a .pyz")
    parser.add_argument(
        "--zipapp-only", action="store_true", help="build only the .pyz (no PyInstaller)"
    )
    parser.add_argument("--onedir", action="store_true", help="a folder, not one file")
    parser.add_argument("--no-clean", action="store_true", help="keep previous output")
    args = parser.parse_args(argv)

    print(f"CCP Forge {version()} — building on {sys.platform}")
    if not args.no_clean:
        clean()

    produced = []
    if not args.zipapp_only:
        produced.append(build_executable(onefile=not args.onedir))
    if args.zipapp or args.zipapp_only:
        produced.append(build_zipapp())

    print("\nbuilt:")
    for path in produced:
        print(f"  {path}  ({os.path.getsize(path) / (1024 * 1024):.1f} MB)")
    print(
        "\nTest it outside this environment:\n"
        f"  {produced[0]} --no-browser --port 8765\n"
        "then open the printed URL."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
