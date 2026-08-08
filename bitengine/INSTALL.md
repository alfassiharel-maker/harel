# Installing and running BitEngine

Three ways to run it, in increasing order of what they need on the machine.

## 1. From the source tree — nothing to install

The engine and CLI import only the Python standard library. With Python 3.11+:

```bash
cd bitengine
python3 cli.py goals
python3 cli.py pack v2.bin out.bite --reference v1.bin
python3 cli.py unpack out.bite restored.bin
python3 -m unittest discover -s tests -t .     # 216 tests
```

## 2. Bundled — one file, still needs Python

```bash
python3 bundle.py                  # -> dist/bitengine.pyz  (~77 KB)
python3 bundle.py --with-ui        # also stages the dashboard
python3 bundle.py --onefile        # also builds a native binary
```

`dist/bitengine.pyz` is a standard-library `zipapp`. Copy it anywhere and run it:

```bash
python3 dist/bitengine.pyz pack v2.bin out.bite --reference v1.bin
./dist/bitengine.sh  pack ...      # macOS / Linux
dist\bitengine.bat   pack ...      # Windows
```

The bundler executes the archive before reporting success, so a build that
cannot run fails at build time rather than on your machine.

## 3. Native binary — no Python required

```bash
pip install pyinstaller
python3 bundle.py --onefile        # -> dist/bitengine  (~6.9 MB)
```

**PyInstaller cannot cross-compile.** It produces a binary for the machine it
runs on, so a Windows `.exe` has to be built on Windows, a macOS binary on
macOS. Build each on its own platform, or in CI with a matrix job. When
PyInstaller is missing the bundler skips this tier with a message instead of
failing the whole build.

## The dashboard

```bash
pip install streamlit pandas
cd bitengine
streamlit run app.py               # http://localhost:8501
```

Or from a bundle: `python3 bundle.py --with-ui`, then `dist/ui/run-ui.sh`
(`run-ui.bat` on Windows) after a one-time
`pip install -r dist/ui/requirements.txt`.

Streamlit is a genuine dependency and cannot be folded into a single file. The
dashboard is a convenience over the CLI, not the product — everything it does is
available from `cli.py` with nothing installed.

### Optional: the comparison panel

```bash
pip install zstandard
```

Enables the `zstd --patch-from` row when you pack a file against a reference.
That is the same-information rival BitEngine is measured against in
[`reports/head_to_head_zstd.txt`](reports/head_to_head_zstd.txt), and on the
measured inputs it produces a smaller file than BitEngine does. The panel is
shown rather than hidden because the alternative is a dashboard that contradicts
the project's own benchmarks.

## Do not copy the files one at a time

`l1.py`, `l2.py` and `l3.py` are one unit, and the dashboard adds `webui.py` and
`app.py` to that set. Copying some of them to a machine and leaving the rest
produces a mixture that fails well below the call site — the reported symptom
was

```
TypeError: Goal.__init__() got an unexpected keyword argument 'keyframe_interval'
```

raised inside `dataclasses`, from an `l2.py` older than the `webui.py` importing
it. `l3.py` now refuses to import against a mismatched set and says which file
is stale, so this fails immediately and legibly instead. The CLI and the
dashboard both inherit that check.

The reliable fix is to stop copying by hand:

```bash
python3 bundle.py --with-ui     # dist/bitengine.pyz + dist/ui/, both consistent by construction
```

`dist/ui/` contains all five files staged together. Copy that whole directory,
not files out of it. On Windows, copy the directory to somewhere like
`C:\bitengine\` and run `run-ui.bat` from inside it — running loose files out of
`C:\Users\User\` is what lets old copies linger and get imported.

## Platform notes

| | Python needed | Command |
| --- | --- | --- |
| Linux / macOS | 3.11+ | `python3 dist/bitengine.pyz …` or `./dist/bitengine.sh …` |
| Windows | 3.11+ | `python dist\bitengine.pyz …` or `dist\bitengine.bat …` |
| Any, native binary | no | `./dist/bitengine …`, built per platform |

Nothing writes outside the paths you name, and no network access is made by the
engine, the CLI or the bundler.

## Verifying an install

```bash
python3 dist/bitengine.pyz goals                       # lists five goals
python3 -m unittest discover -s tests -t .             # 216 tests
python3 bench_l1.py --size 16MB --block 64KB           # every row hash-checked
python3 bench_real.py --head-to-head --repository ..   # BitEngine vs zstd
```

`pack` re-opens every container it writes and decodes it against the input's
SHA-256 before reporting a saving, exiting non-zero if it does not match. That
check is not optional and not a separate flag.
