# Scout

A local CLI that analyses one technical or business domain and reports ranked
problems and opportunities. The specification and architecture are in
[`SPEC.md`](SPEC.md).

```
domain → domain map → candidate problems → deterministic ranking → report
```

## Run it

Offline, no API key, no dependencies — replays a recorded payload:

```sh
python3 -m scout "תשתית AI" --fixture tests/scout/fixtures/ai-infrastructure.json
```

Against a live provider:

```sh
export ANTHROPIC_API_KEY=...        # environment only; never a flag or a file
pip install anthropic
python3 -m scout "AI infrastructure"
```

Each run writes `<slug>-<timestamp>.md` and `.json` to `scout/runs/` and prints
the Markdown path. `--print` also writes the report to stdout.

| Flag | Purpose |
|---|---|
| `--count N` | candidate problems to ask for (default 8) |
| `--effort` | `low`…`max` reasoning depth (default `high`) |
| `--max-tokens` | per-call ceiling covering thinking *and* output (default 32000) |
| `--fixture PATH` | replay a recorded payload; no provider, no key |
| `--out DIR` | where to write the run |

## Tests

Standard library only. Nothing here needs a key or a network:

```sh
python3 -m unittest discover -s tests/scout -t .
```

## What the report guarantees

**Nothing is source-verified.** The MVP has no web access, so every problem
separates its claims into three fields that are never merged: `known_facts`
(model-asserted domain knowledge), `hypotheses` (the analytical leap), and
`open_questions` (what to verify before acting). Hedged language in the fact
bucket is rejected outright — a hedge is a hypothesis wearing a fact's clothes.

**Depth is enforced, not requested.** A problem reaches the report only with a
causal `mechanism`, a named `who_pays`, a `current_workaround`, and an observable
`signal`. Candidates that fail are printed under *insufficient evidence* with
their reasons, so a thin run cannot pass for a thorough one. The specification's
own bad example — "an AI company uses cloud" — scores 5/5 on four dimensions and
is still kept out; that case is in the test suite.

**Rankings are explainable.** Scoring is deterministic, weights are named
constants with a written rationale, and every score reports its per-dimension
drivers. A candidate missing any dimension scores `None`, never a plausible low
number.

## Layout

```
scout/
  cli.py         arguments, stdout, exit codes
  pipeline.py    analyse → generate → score → render → save
  llm.py         the only network boundary; LanguageModel protocol + Mock
  prompts.py     the two prompts and their JSON schemas
  models.py      validation — where every rule with teeth lives
  scoring.py     deterministic ranking; stdlib only, zero project imports
  report.py      dataclasses → Markdown
```

`tests/scout/test_layering.py` enforces that contract by reading the imports, so
a new module has to declare its dependencies or the build fails.

## Not in this version

Web and news evidence retrieval, competitor and funding lookup, a database, a
UI, scoring calibration against real outcomes. See `SPEC.md` §1.4.
