# CCP Forge — the application

Describes working code in `ccp/integration/` and `ccp/ui/`, and the packaged
product in `packaging/`. Sections are marked **IMPLEMENTED**, **NOT SUPPORTED**,
or **OPEN**. Nothing marked OPEN in `docs/01-runtime.md` or
`docs/02-product-engine.md` has been quietly turned into supported behaviour by
this layer.

---

## 1. What the user does

```
Launch CCP Forge
   ↓  choose a project folder, or drop a .zip
CCP analyses the input          (files, sizes, what will be skipped and why)
   ↓
CCP builds the representation   (shared structure, bases, change programs)
   ↓
CCP verifies the artifact       (every unit reconstructed, digest-checked)
   ↓
Artifact overview               (sizes, saving, units, groups, verification)
   ↓
Browse units and groups · selective read · full materialization · export
```

No Python, no terminal, no CLI, and no knowledge of Core, Runtime, API, units,
change programs or reconstruction is required at any point.

## 2. Architecture — IMPLEMENTED

```
User
  ↓
ccp.ui            application: controller (state machine), local host, interface
  ↓
ccp.integration   input adapters, analysis, build pipeline, workspace
  ↓
ccp.product       artifact lifecycle, typed errors, observability
  ↓
ccp.api           the stable public surface
  ↓
ccp.runtime       load, verify, materialize, selective read, accounting
  ↓
ccp.capabilities  selective reads over the representation
  ↓
ccp.core          representation, base selection, change programs
```

Dependency direction is one-way and enforced by import: neither `ccp.ui` nor
`ccp.integration` imports `ccp.core`, and no layer above the Core reconstructs
anything itself. **There is exactly one reconstruction implementation**, in
`ccp.core`, reached through the Runtime.

### The application is split in two, deliberately

`ccp/ui/controller.py` is the entire application without a view: state machine,
artifact ownership, every operation. `ccp/ui/server.py` and `web.py` are the
window onto it. That split is why the whole product workflow is testable in a
headless environment — the tests drive the controller directly, and separately
drive the HTTP surface over a real socket.

## 3. ENGINEERING DECISION — the window is the system browser

CCP Forge runs a local application host on `127.0.0.1` and opens the user's
browser at it. The reasons, recorded so the choice is not mistaken for an
accident:

* the project is stdlib-only, and no GUI toolkit is guaranteed present (this
  build environment has no `tkinter` and no display at all);
* the interface then behaves identically on Windows, macOS and Linux;
* it is the only option that can be driven end to end in automated tests without
  a display.

The user does not see any of this: they launch the application and a window
opens. Security posture, because this is a process listening on a user's machine:

* binds **127.0.0.1 only** — never a routable address;
* every request must carry a **session token** minted at startup, so another
  local process cannot drive the user's artifacts by guessing the port;
* a strict `Content-Security-Policy`, no external stylesheet, script, font or
  image, and **no outbound network access of any kind**.

## 4. Input — IMPLEMENTED

| Input | Notes |
| --- | --- |
| project folder | every file is one unit, keyed by relative path |
| `.zip` archive | the input the product direction names first; drag-and-drop supported |
| `.ccp` artifact | reopen something built earlier |

`.git`, `.hg`, `.svn`, `__pycache__`, `.venv`, `venv` are excluded — a
version-control directory would bury the real files under thousands of objects.

**Untrusted input is treated as untrusted.** Zip entries naming absolute paths,
`..` traversal, or symlinks are skipped with a recorded reason; an archive whose
expansion ratio exceeds 200× is refused before a single entry is decompressed;
per-file, total-size and file-count limits are checked *before* a build starts,
so an input that cannot be completed safely is refused with a reason rather than
started and then killed by the allocator.

## 5. Resource management — IMPLEMENTED

* **Explicit lifecycle** — one artifact at a time; building or opening another
  closes the previous one; `close()` is idempotent; every operation on a closed
  artifact raises a typed `ResourceError`.
* **Bounded memory** — input limits (200k units, 2 GiB total, 512 MiB per unit),
  an artifact-size ceiling on open, an 8 MiB cap on a single selective read, a
  512 MiB cap on an uploaded archive, and a 1 MiB cap on any JSON request body.
* **No accidental full materialization** — a selective read never materialises
  the unit, and asking for more than the selective cap is refused with a message
  pointing at full materialization rather than silently doing it.
* **Deterministic cleanup** — the upload directory is a `TemporaryDirectory`
  owned by the server and removed on shutdown; every file write closes its
  handle in a `finally`; a failed write removes its own temporary file.
* **No hidden global state** — controllers, engines and artifacts are plain
  instances. Two controllers share nothing.

## 6. Concurrency — the contract is respected, not widened

The semantic contract lists concurrent access to one artifact instance as
**unsupported**, and that has not changed. A user interface is inherently
concurrent, so the controller holds a lock across every artifact operation: the
artifact still only ever sees one caller at a time. Concurrent access to a single
artifact remains **OPEN**; what is implemented is serialisation, not concurrency.

## 7. Selective reconstruction is a product feature — IMPLEMENTED

The interface makes the distinction explicit, and never blurs it:

* **SELECTIVE RECONSTRUCTION** — `read_range`, executing only the instructions
  covering the window. Verification state is reported as `slice (unverified)`,
  because a digest covers a whole unit and a slice cannot re-check it.
* **FULL MATERIALIZATION** — the whole unit, reported as `digest checked`.

Every read reports **measured** work, taken from the Runtime's accounting and
never estimated: bytes requested, bytes returned, bytes touched, work ratio,
units touched, instructions visited / total, verification state, elapsed time.

## 8. Verification semantics — unchanged

`verify()` reconstructs every unit and checks it against the digest recorded at
build time. The application never claims a selective read independently verifies
a unit. A build runs verification automatically before the artifact is shown, so
what the user sees has already been checked end to end.

## 9. Errors — IMPLEMENTED

Every failure reaches the user as **what happened, why, and what to do next**,
derived from the typed product error model. Internal exceptions never become the
primary message and tracebacks are never sent to the interface — but nothing is
hidden either: an unexpected internal failure is still shown, framed as a fault
in the application rather than as the user's mistake.

Covered: malformed artifact · invalid input · invalid unit · invalid range ·
verification failure · unsupported operation · resource failure · filesystem
errors · out of memory · cancelled operation · unexpected internal failure.

After any recoverable failure the application stays usable: a bad range does not
cost the user their build.

## 10. Artifact management — IMPLEMENTED

Create · open · inspect · verify · browse · read · materialize · export · close.

Writing is **atomic**: content goes to a temporary file in the destination
directory and is then renamed into place, so an interrupted write cannot leave a
truncated artifact where a good one was. An existing file is **never silently
overwritten** — overwriting must be requested. Export paths are resolved through
a guard that refuses absolute paths, drive letters, `..` segments, and anything
that resolves outside the destination (including via an existing symlink).

## 11. Application state — IMPLEMENTED

`no_input · analysing · building · verifying · ready · reading · materializing ·
exporting · error · closed`, reported in one consistent snapshot so the interface
can never render a mixture of two states.

## 12. Packaging — IMPLEMENTED

```bash
python3 -m pip install -r packaging/requirements-build.txt
python3 packaging/build_app.py            # native executable
python3 packaging/build_app.py --zipapp   # and a stdlib-only .pyz
```

| Output | What it is |
| --- | --- |
| `dist/CCPForge` / `dist/CCPForge.exe` | single-file executable, bundles a Python runtime — **the end user installs nothing** |
| `dist/CCPForge.pyz` | 228 KB stdlib-only zipapp; needs Python 3.9+ on the machine |

The application itself has **no runtime dependencies**: `ccp` imports the
standard library and nothing else. PyInstaller is build-time only.

**Windows.** PyInstaller builds for the platform it runs on and cannot
cross-compile, so `CCPForge.exe` is produced by running the same command on a
Windows host. That is the architecture the product direction already specifies —
the development environment and the target build environment are different
machines — and `build_app.py` is what the target machine runs. Windows executable
metadata (version, product name, icon if present) is emitted by the same script.
**The Linux executable in this repository was built and tested; a Windows .exe
has not been produced here, because this environment is Linux.**

Application data lives in the platform's conventional per-user location
(`%LOCALAPPDATA%\CCPForge`, `~/Library/Application Support/CCPForge`, or
`$XDG_DATA_HOME/CCPForge`) — never beside the executable, never a developer path.

## 13. Development vs production — IMPLEMENTED

The packaged application contains `ccp` only. `ccp/tests`, `ccp/benchmarks` and
`ccp/docs` are excluded from the zipapp; `experiments/ccp` is research and is not
imported by any product code path. There are no developer-specific absolute
paths and no dependency on local machine state.

The CLI (`python3 -m ccp.cli`) is unchanged and still routes through `ccp.api`.
The application and the CLI share the same Product/API layers; neither has its
own CCP implementation.

## 14. NOT SUPPORTED

* Editing an artifact after it is built (add / remove / update a unit).
* Returning content that is not the original bytes.
* Executing a target program, or deciding how one should run.
* Opening an artifact larger than memory.
* Any network, cloud or multi-user capability. The product is local-only.

## 15. OPEN

Carried forward unchanged, and not implemented behind the user's back:

* **Concurrent access to one artifact instance** (see §6).
* **A looser semantic contract**, where output may legitimately differ from the
  original bytes.
* **Lazy / memory-mapped loading**, so an artifact larger than memory can be
  opened.
* **Execution management** — the part of the original CCP direction that would
  have CCP manage *how a program runs*. `experiments/ccp/FINDINGS_EXECUTION.md`
  records what was measured about it; nothing in the product claims it.
* **A Windows build produced and tested on Windows** (see §12).
