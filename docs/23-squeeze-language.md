# 23 — Squeeze: the footprint policy language

**Status:** implemented · **Date:** 2026-08-21 · **Code:** `backend/squeeze/`,
`policies/*.sqz` · **Tests:** `tests/squeeze/` (98, dependency-free)

Squeeze is a small declarative language — code *about* code — whose sources
(`*.sqz`) state how much room the product's data is allowed to occupy: on disk,
in memory, in object storage and in model weights. The compiler turns a source
into a **plan**: an auditable footprint estimate with the drivers that produced
it, the limits it was checked against, and an explicit list of what it could not
compute.

Nothing in the compiler compresses or deletes anything. A plan is reviewed and
applied by a person, exactly like the SQL in `database/migrations/`.

---

## 1. Why a language

The alternative — a spreadsheet, or a `footprint.yaml` read by a script — was
rejected for four reasons, each of which is now a compile-time property:

| Problem with the spreadsheet | What Squeeze does |
|---|---|
| `30 days` and `30 GiB` are both "30" | Units are typed; dimensions cannot be mixed (`units.py`) |
| Retention rules live in prose and drift from the schema | A window into an undeclared tier, or a cold window shorter than the warm one, is a compile error |
| Ratios are aspirations nobody re-measures | `impl` names a real codec and `squeeze verify` measures it, failing the build on optimistic drift |
| A total with a hole in it looks like a total | An unmeasured class is `unknown`; `coverage` drops below 1 and every limit check becomes `None`, never `True` |

The fourth is the one that decided it. A footprint estimate is used to sign off
capacity and price a subscription; a number that is confidently wrong because one
class was never measured is worse than no number. This mirrors the analytics
engine's rule that missing data is `None`, never `0`.

## 2. Shape of a source

```
version 1
currency ILS

codec  <name> { <setting>… }
tier   <name> { <setting>… }
class  <name> : <kind> { <setting>… }
policy <name> { <setting>… }
```

One setting per line; `;` is an explicit separator so a short declaration fits on
one line. `#` starts a comment. `=`, `[ ]` and `,` are accepted where they read
naturally and carry no meaning: `codecs [a, b]` and `codecs a b` are the same
list.

### Grammar

```ebnf
file        = { preamble | block } ;
preamble    = ("version" number | "currency" word) NEWLINE ;
block       = keyword name [ ":" kind ] "{" { setting } "}" ;
keyword     = "codec" | "tier" | "class" | "policy" ;
setting     = word [ "=" ] { atom } NEWLINE ;
atom        = number [ unit ] | word | string | operator ;
NEWLINE     = "\n" | ";" ;
```

### Units

| Dimension | Units |
|---|---|
| bytes | `bytes` `B` `KiB` `MiB` `GiB` `TiB` (1024ⁿ) and `KB` `MB` `GB` `TB` (1000ⁿ) |
| time | `us` `ms` `s` `min` `h` `day(s)` `week(s)` `month(s)` `year(s)` |
| count | `records` `rows` `users` `messages` `params` |
| money | `minor` — minor units of the file's `currency` |

Both binary and decimal byte prefixes exist because a memory limit is quoted in
GiB and an object-storage bill in GB; conflating them understates a cold tier by
7%. A `month` is exactly 30 days and a `year` exactly 365. The compiler has no
calendar on purpose: retention arithmetic that depended on which months a window
spanned would compile the same source to different plans on different days.

Magnitudes are exact rationals (`fractions.Fraction`) throughout. `1.2 KiB` is
1228.8 bytes exactly, and two runs over the same source produce byte-identical
totals — which is what makes a plan diffable in review.

## 3. Declarations

### `codec`

```
codec delta_varint {
  kind        lossless          # or `lossy`, which then requires `fidelity`
  ratio       6.4               # input bytes / output bytes, measured
  cpu         0.6 ms per MiB    # normalised to per-MiB internally
  applies_to  timeseries, relational
  impl        delta_varint      # implementation in implementations.py
}
```

`kind` is never inferred: it decides whether clinical classes may use the codec.
A lossless codec gets `fidelity 1`; a lossy one without a declared fidelity is an
error, because `require fidelity >= x` could not be checked against it. A codec
with no `ratio` is reported as *unknown* rather than rejected — it is declared but
unusable by the planner until someone measures it.

### `tier`

```
tier warm {
  medium      disk              # memory | disk | object | archive
  latency     60 ms             # read-latency budget, optional
  unit_cost   95 minor per GiB per month
  codecs      delta_varint, zlib6, lzma6, int8
}
```

`unit_cost` has exactly one accepted shape. Comparing tiers requires a common
denominator, and accepting three spellings of the same price would mean the
comparison silently depended on which one an author used.

### `class`

```
class activity_streams : timeseries {
  record      24 bytes
  growth      864000 records per user per month
  retain      hot 7 days, warm 13 months, cold 5 years
  sensitivity personal          # routine | personal | clinical
}
```

Kinds: `timeseries`, `relational`, `document`, `blob`, `tensor`, `context`. The
kind decides which codecs are candidates at all — a delta encoder is excellent on
a monotonic sensor series and useless on an already-compressed blob.

`sensitivity clinical` is load-bearing: the planner refuses lossy codecs on such
a class. Readiness, HRV and injury-risk inputs feed numbers an athlete makes
training decisions on, so storing an approximation of a measured value is not a
trade the compiler is allowed to make for bytes.

`retain` windows are **cumulative**, and each must be at least as long as the one
before it. `hot 7 days, warm 13 months` means the hot tier holds days 0–7 and the
warm tier holds day 7 to month 13; residency in a tier is therefore the
*difference* between consecutive windows.

### `policy`

```
policy footprint_v1 {
  objective   minimise bytes    # bytes | cost | latency
  population  25000 users
  horizon     18 months
  limit       total 6 TiB
  limit       per_user 220 MiB
  require     fidelity >= 0.99
  require     explanation
  covers      activity_streams, daily_metrics, …
}
```

`horizon` clips residency: a 5-year cold window cannot hold more than 18 months
of data 18 months after launch, and a plan that claimed otherwise would
over-provision the largest tier by years.

`require explanation` is a promise the plan makes to its reader — if no
allocation could be sized, the plan has no drivers and the compiler says so.

## 4. Planning

For each (class, tier) residency the planner filters the tier's codecs to those
*provably* usable, then picks one under the objective. A codec is rejected — and
the reason is kept and printed — when it does not apply to the class's kind, has
no measured ratio, is lossy on a clinical class, falls below a required fidelity,
or cannot be shown to fit the tier's latency budget. **Unmeasured fails closed:**
a codec with no `cpu` figure cannot satisfy a latency budget, so it is treated as
violating it rather than as satisfying it.

Selection is a total order, so the same source always compiles to the same plan:

| Objective | Primary | Tie-breaks |
|---|---|---|
| `bytes`, `cost` | highest ratio | lowest CPU, then name |
| `latency` | lowest CPU | highest ratio, then name |

`bytes` and `cost` share a key because a tier prices a stored byte at a fixed
rate: on one tier the cheapest plan is the smallest one.

Arithmetic, in full:

```
residency    = min(window - previous_window, horizon - previous_window)
records      = growth.amount × residency / growth.period
raw_bytes    = record_size × records × (population if scope is user/account else 1)
stored_bytes = ceil(raw_bytes / ratio)
```

Byte counts round **up**: a plan compared against a hard limit should never
under-report. A user-scoped class with no `population` yields `None`, not a
per-user figure passed off as a fleet total.

### Output

```
  totals      raw 8.746 TiB -> stored 1.36 TiB (6.43x), saved 7.386 TiB
  per athlete 56.99 MiB
  monthly     166474 minor ILS at the stored size
  coverage    100.0% of allocations sized

  limits
    per_user         220 MiB actual    56.99 MiB   ok
    total              6 TiB actual     1.36 TiB   ok

  drivers (bytes saved; these sum to the total saving)
     1. activity_streams@warm          5.079 TiB   68.8%   delta_varint on disk
     2. activity_streams@cold          1.989 TiB   26.9%   delta_varint on object
     …
```

Two contractual properties, both covered by tests:

* **Drivers sum to the total.** `sum(driver.saved_bytes) == plan.saved_bytes`
  exactly, the same discipline readiness drivers hold to in `docs/04`.
* **Unknowns stay unknown.** `coverage < 1` makes every limit check `None` and
  `plan.is_provable` false. One unpriced tier makes the monthly bill `None`
  rather than a partial figure presented as a total.

## 5. Verification

`squeeze verify` runs each codec's `impl` over a deterministic sample and reports
drift against the declaration. Implementations (`implementations.py`, standard
library only):

| `impl` | What it is | Lossless |
|---|---|---|
| `delta_varint` | first-order delta + zigzag varint over int64 series — the part of the Gorilla encoding (Pelkonen et al., VLDB 2015, §4.1) that pays for itself without bit packing | yes |
| `zlib`, `lzma`, `bz2` | standard library general-purpose codecs at production settings | yes |
| `int8_quantise` | affine per-tensor quantisation to int8, header carrying minimum and scale | no — fidelity is measured, not nominal |
| `raw` | identity, for measuring an uncompressed baseline | yes |

Drift is only ever reported when a declaration is **optimistic** (declared more
than 10% above measured); understating a codec is the safe direction and is
allowed. Round-trip failure fails the check outright. A codec with no sample for
its kinds is `unverified`, which `--strict` treats as a failure — not as a pass.

The built-in samples are shaped like the platform's real payloads (a 20-minute
one-second heart-rate stream, an activity JSON document, a small weight tensor)
and are generated by closed-form recurrences with no randomness: a footprint
check that produced a different number each run would be dismissed as noise.

## 6. Commands and the CI gate

```bash
python3 -m backend.squeeze check  policies/footprint.sqz
python3 -m backend.squeeze plan   policies/footprint.sqz [--policy NAME] [--json] [--strict]
python3 -m backend.squeeze verify policies/footprint.sqz [--strict]
```

Or `make sqz-check` / `sqz-plan` / `sqz-verify` / `sqz-gate`. Exit codes: `0`
clean, `1` errors or an unproven plan under `--strict`, `2` usage.

CI runs the gate in the zero-dependency job, alongside the analytics suite:
`plan --strict` fails on an exceeded limit **and** on an unprovable one, and
`verify --strict` fails on drift. A policy whose coverage slipped below 100% has
stopped showing that the product fits in its storage budget, which is the thing
the gate exists to catch.

## 7. Diagnostics

Codes are stable, so a pipeline can allow one specific warning without allowing
warnings in general. The first two digits name the phase: `01` lexer, `02`
parser, `03` checker, `04` planner, `05` verifier.

Three severities, and the third is the point of the design:

| Severity | Meaning |
|---|---|
| `error` | the source is wrong; no program, no plan |
| `warning` | valid but suspicious — e.g. a tier where no codec applies to a class's kind |
| `unknown` | not a defect: something the plan could not compute. Reported so the reviewer sees the gap instead of a plausible zero |

The compiler collects diagnostics and keeps going where recovery is unambiguous:
one malformed line costs one diagnostic, not the rest of the file, because these
files are reviewed in one pass.

## 8. Layering

`backend/squeeze/` imports **nothing** from this project and no third-party
package, enforced by an import-linter contract in `pyproject.toml` and by the
zero-dependency CI job. It is `mypy --strict`. The reason is the same one that
keeps `backend/algorithms/` pure: the compiler must run in a migration review, in
the fastest CI job, and on a laptop with nothing installed. It reads text and
returns data structures; if it ever needs to know about a session, a request or an
ORM model, the design is wrong.

## 9. What this deliberately does not do

* **It does not apply anything.** No compression, no deletion, no tier migration,
  no `ALTER TABLE`. A plan is an input to a reviewed change, and per the
  repository's database rule, a person executes it.
* **It does not measure production.** `verify` measures a sample. Pointing it at
  exported rows is a deliberate act with a real export, not something CI does to
  a live database.
* **It does not convert currency.** An FX rate is a runtime fact; a compiler that
  embedded one would emit plans that were wrong the next day.
* **It does not model per-channel quantisation, dictionary sharing across rows,
  or index overhead.** Each would improve the estimate and none can be verified
  from a flat sample today. When one becomes worth having, it arrives as a new
  setting with a `version` bump, not as a silent change in what an existing
  source means.

## 10. Open questions

1. **Where do growth rates come from?** They are planning assumptions today
   (`docs/10`). They should be derived from measured table sizes once the
   training module is in production, and the source should record which release
   the measurement came from.
2. **Should a plan be stored?** A `analytics.footprint_plans` table would let
   drift be tracked over releases. That needs a reviewed migration and an owner;
   until then the plan lives in the pull request that changed the policy.
3. **Per-athlete limits and the entitlement tier.** `limit per_user` is one
   number; a paid tier plausibly buys a larger stream history. That is a policy
   per entitlement, which the language can already express by compiling two
   policies — but nothing yet ties a policy name to an entitlement.
