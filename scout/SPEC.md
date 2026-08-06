# Scout — Business Problem & Opportunity Analyzer

A **local** CLI tool. Input: one technical or business domain. Output: a ranked
report of real problems and opportunities in that domain.

> Status: **specification + architecture, awaiting approval.** No service code
> written yet, per the repository engineering rule.

---

## 1. Specification (MVP)

### 1.1 The one job

```
input: a domain string  ("AI infrastructure", "cyber", "cloud", "robotics")
   ↓
domain analysis   (structure / engineering / business)
   ↓
problem generation  (candidates derived from the analysis, not invented)
   ↓
deterministic ranking
   ↓
report: Markdown + JSON on disk
```

Nothing else is in the MVP. No web UI, no database, no accounts, no history
browsing, no multi-domain comparison, no export formats beyond MD/JSON.

### 1.2 What the user gets

**A. Domain structure**
- how systems in the domain are architected
- which components exist
- how data flows between them

**B. Engineering side**
- processes as they are performed today
- bottlenecks
- resource waste
- what is still done inefficiently

**C. Business side**
- users and paying customers
- where companies spend money
- which costs are high
- which processes could improve

**D. Ranked problem list.** Each problem is scored on five dimensions:

| Dimension | Meaning | Scale |
|---|---|---|
| `severity` | how badly it hurts when it happens | 0–5 |
| `cost` | money/time it burns today | 0–5 |
| `breadth` | how many people or companies are affected | 0–5 |
| `solution_maturity` | how well existing solutions already cover it | 0–5 (**inverse** — high maturity lowers the score) |
| `startup_potential` | can this become a company | 0–5 |

### 1.3 The depth requirement — enforced, not requested

A problem is only accepted into the report if it carries **all** of these
fields. This is the mechanism that stops `"an AI company uses cloud"` and
forces `"a company runs large models on GPUs for long stretches and pays for
capacity while utilisation is partial — is there an opportunity here?"`

| Field | Why it is mandatory |
|---|---|
| `mechanism` | *why* the waste/friction happens, causally. Not a restatement. |
| `who_pays` | the specific role or company type carrying the cost |
| `current_workaround` | what people do today instead — proves the problem is lived, not theoretical |
| `signal` | an observable thing a reader can go check themselves |
| `confidence` | `high` / `medium` / `low` — the model's own honesty flag |

Any candidate missing a field, or with a `None` dimension, does **not** get a
score. It is listed separately under *insufficient evidence*. Per the repo
rule: **missing data is `None`, never `0`** — we never invent a plausible
number to make a row look complete.

Numbers are labelled: either an `estimate` with its stated basis, or `None`.
The MVP has **no web access**, so it makes no citations and claims no
statistics as fact. Evidence retrieval is explicitly Phase 2.

### 1.3.1 Epistemic separation — the three buckets

Every problem separates its claims into three fields that are **never merged**,
in the payload or in the report:

| Bucket | Field | What belongs in it |
|---|---|---|
| Known fact | `known_facts` | established domain knowledge, stated flatly |
| Hypothesis | `hypotheses` | the analytical leap — where the opportunity is asserted |
| Direction to check | `open_questions` | what a reader must verify before acting |

Enforcement is deterministic, in `models.py`, not a polite request in a prompt:

* all three buckets must be non-empty — a problem with no hypothesis is a
  description, and one with nothing to verify is a fabrication;
* a `known_facts` entry containing a hedge marker (*probably, likely, might,
  כנראה, ייתכן*, …) is rejected — hedged language is a hypothesis wearing a
  fact's clothes, and that is precisely the mixing this rule forbids.

Because the MVP has no web access, `known_facts` means *model-asserted domain
knowledge, unverified against a source*, and the report labels it that way.
Nothing here is presented as sourced.

### 1.4 Out of scope for MVP (deliberately)

Web/news evidence retrieval, patent & funding lookup, competitor scan,
multi-round debate between analyst personas, persistence in a database,
scoring calibration against real outcomes, a UI.

---

## 2. Architecture

Deliberately small: four modules, one of which touches the network.

```
cli.py ──▶ pipeline.py ──▶ llm.py      (the only network boundary)
                    ├────▶ scoring.py  (pure, stdlib only, no imports from us)
                    └────▶ report.py   (rendering only)
```

```
scout/
  __init__.py
  cli.py            argument parsing, stdout, exit codes. No logic.
  pipeline.py       orchestration: analyse → generate → score → render → save
  llm.py            Anthropic client. Retries, stop_reason check, usage/cost.
  prompts.py        the two prompts, as constants
  models.py         dataclasses + validation of the model's JSON
  scoring.py        deterministic ranking. stdlib only. Zero project imports.
  report.py         dataclasses → Markdown
  runs/             output: <slug>-<utc-timestamp>.{json,md}   (gitignored)
tests/scout/
  test_scoring.py   anchor case, undefined case, one hand-computed value
  test_models.py    accepts a good payload, rejects each malformed one
  test_report.py    fixture → rendered Markdown, no network
  fixtures/         a recorded analysis payload
```

### 2.1 Provider abstraction

`llm.py` defines a narrow `LanguageModel` protocol — one method, JSON in / JSON
out — with two implementations: `AnthropicModel` and `MockModel` (fixture
replay, used by tests and by any offline run). Nothing above `llm.py` knows
which provider is behind it, so a different provider is a new class and no
other change.

Steps 1–2 have **no dependency on a key at all**. Step 3 reads
`ANTHROPIC_API_KEY` from the environment only — never a flag, never a file, and
no secret is ever committed.

### 2.2 Model interaction

Two calls per run, both returning strict JSON:

1. **analyse** — domain → structure + engineering + business.
2. **generate** — that analysis → problem candidates with the five dimensions
   and the five mandatory depth fields.

Split in two because the second call must reason *over* a finished map rather
than produce map and conclusions in one breath — that is where shallow output
comes from.

Per repo AI conventions: model `claude-opus-5`; depth via
`output_config.effort`; no `temperature`/`top_p`; `stop_reason` checked before
reading content; provider, model, tokens and cost recorded on every run
record.

### 2.3 Scoring

```
score = 100 × ( 0.30·severity
              + 0.25·cost
              + 0.20·breadth
              + 0.15·startup_potential
              + 0.10·(5 − solution_maturity) ) / 5
```

Weights are named constants with a written rationale. The function returns the
score **and its drivers** (per-dimension contribution), per repo rule — so a
ranking is always explainable. Any `None` dimension ⇒ the whole score is
`None`.

`scoring.py` imports nothing and needs nothing installed:
`python3 -m unittest discover -s tests -t .` must pass on a bare machine.

### 2.4 Storage & config

No database. Each run writes two files under `scout/runs/`. Configuration is
`ANTHROPIC_API_KEY` from the environment only — never a flag, never a file, and
the key is never written into a run record or a log.

### 2.5 Build order — each step working before the next

| Step | Deliverable | Proof it works |
|---|---|---|
| 1 | `models.py` + `scoring.py` + tests | `unittest` green with zero deps installed |
| 2 | `report.py` + fixture | a full Markdown report renders offline, no API key |
| 3 | `llm.py` + `prompts.py` + `cli.py` | a real end-to-end run on one domain |

Step 3 is the only step that needs a key. Steps 1–2 are verifiable now.
