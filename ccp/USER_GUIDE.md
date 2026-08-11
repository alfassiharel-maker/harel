# CCP Forge — user guide

CCP Forge takes a project, finds the structure its files share, and stores one
copy of that shared structure plus the changes each file makes to it. The result
is a single **artifact** you can browse, read from, verify, and export — and you
can read *part* of a file out of it without rebuilding the whole file.

You do not need Python, a terminal, or any knowledge of how CCP works.

---

## Install

**Windows** — download `CCPForge.exe` and double-click it. Nothing to install.

**macOS / Linux** — download `CCPForge`, make it executable, and run it:

```bash
chmod +x CCPForge
./CCPForge
```

A console window opens, prints the application address, and your browser opens on
the CCP Forge window. **Keep the console window open while you work** — closing
it quits the application.

> If your browser does not open by itself, copy the address from the console
> window (it looks like `http://127.0.0.1:53421/?t=…`) and paste it in.
> The address includes a one-time key, so use the whole thing.

CCP Forge runs entirely on your machine. It listens only to your own computer and
never connects to the internet.

## First use, in one minute

1. **Choose input.** Type or paste the path to a project folder in the
   *Project folder or .zip path* box — or drag a `.zip` onto the drop area.
2. **Press "Build artifact".** CCP analyses the input, builds the representation,
   and verifies every unit. Progress is shown as it goes; you can cancel.
3. **Read the overview.** Original size, artifact size, saving, how many units,
   how many shared bases, and whether verification passed.
4. **Browse units.** Filter by name, click one to select it.
5. **Read from it.** Set an offset and length and press **Selective read**, or
   press **Full materialization** for the whole thing.
6. **Save.** Press **Save** to write the artifact as a `.ccp` file, or
   **Export unit** to write one file's exact original bytes.

## What CCP Forge is good at — and what it is not

CCP finds **shared structure between files**. It pays off when your input
contains many near-identical files: several releases of a product, a set of model
checkpoints, per-customer variants of one configuration, successive revisions of
a codebase.

It is **not a general-purpose compressor**, and is not a replacement for one.
Measured on this project's own source tree:

| Input | Result |
| --- | --- |
| 5 revisions of one codebase (723 KB, 111 files) | **37.74% smaller** |
| a single revision (369 KB, 55 files) | **0.86% larger** |

The second row is not a defect and is not hidden — a single set of unrelated
files has no shared structure to find, and the artifact then costs slightly more
than the input because of its index. If your input is one copy of everything,
CCP Forge is the wrong tool.

## Supported inputs

| Input | Notes |
| --- | --- |
| A project folder | Every file becomes one unit. `.git`, `__pycache__`, `.venv` and similar are skipped. |
| A `.zip` archive | Drag it onto the window, or give its path. Unsafe entries (absolute paths, `..`, symlinks) are skipped and listed. |
| A `.ccp` artifact | Reopen anything you saved earlier. |

Limits: up to 200,000 files, 2 GiB of content, 512 MiB per file. An input over a
limit is refused up front with the reason, rather than failing part-way through.

## Selective read vs full materialization

This is the distinction the product is built around, and the interface labels it
on every result.

**SELECTIVE RECONSTRUCTION** — you ask for a window (an offset and a length), and
CCP executes only the instructions needed to produce that window. Reading 256
bytes from the middle of a 16 KB file touches 256 bytes, not 16 KB. The whole
file is never rebuilt.

**FULL MATERIALIZATION** — you ask for the entire unit. CCP rebuilds it and
checks the result against the digest recorded when the artifact was built.

Every result shows what it actually cost — bytes requested, bytes returned, bytes
touched, work ratio, instructions executed, and time. These are measured, not
estimated.

### Why a selective read says "slice (unverified)"

An artifact stores one digest per **whole unit**. A full materialization can be
checked against that digest, so it reports *digest checked*. A slice of a unit
cannot be — there is no digest for a fragment. It reports *slice (unverified)*,
and that is a statement about what was checked, not a warning that something is
wrong.

To confirm an artifact is intact, press **Verify**: it rebuilds every unit and
checks each against its digest. A build does this automatically before showing
you the artifact.

## Saving and exporting

* **Save** writes the artifact as a `.ccp` file. You can reopen it later, and
  recently saved artifacts are offered on the start screen.
* **Export unit** writes one unit's exact original bytes to a folder you choose.

Both write atomically: content goes to a temporary file first and is renamed into
place, so an interrupted write can never leave a damaged file where a good one
was. Nothing is overwritten silently.

By default artifacts go to your user application-data folder:

| Platform | Location |
| --- | --- |
| Windows | `%LOCALAPPDATA%\CCPForge\artifacts` |
| macOS | `~/Library/Application Support/CCPForge/artifacts` |
| Linux | `~/.local/share/CCPForge/artifacts` |

## When something goes wrong

Every error tells you what happened, why, and what to do next. The application
stays usable — a bad request never costs you your artifact. Common cases:

| Message | What it means |
| --- | --- |
| *That input can't be used* | The folder or archive is missing, empty, the wrong kind, or over a limit. |
| *This file isn't a CCP artifact* | Not a `.ccp` container, or damaged since it was written. |
| *That range isn't valid* | Offset and length must both be zero or greater. |
| *Verification failed* | A unit did not match its digest. The artifact is damaged — rebuild it and do not use the copy. |
| *Couldn't save that* | The destination exists, is read-only, is full, or is outside the allowed folder. |

## Limitations in this version

* An artifact is **read-only** once built. To change it, rebuild from the input.
* An artifact must fit in memory to be opened.
* One artifact at a time per window.
* Everything CCP Forge returns is **byte-identical** to your original input. It
  never returns approximate or transformed content.

## For developers

The command line is unchanged and available:

```bash
python3 -m ccp.cli build <folder> --out model.ccp
python3 -m ccp.cli stat model.ccp
python3 -m ccp.cli read model.ccp <unit> --offset 8000 --length 256
python3 -m ccp.cli contract
```

Launch the application from source with `python3 -m ccp`, run the tests with
`python3 -m unittest discover -s ccp/tests -t .`, and build the distributable
with `python3 packaging/build_app.py`. Architecture and contracts are in
`ccp/docs/`.
