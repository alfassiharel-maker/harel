# 05 — AI Coach Architecture

**Status:** awaiting review

The differentiator is not "an LLM answers sports questions" — anyone can ship
that. It is that **the model reasons over this athlete's computed, explained
metrics** and can only say things the data supports.

```
Raw provider data
      ↓  ingest + data-quality checks
Normalised activities and wellness
      ↓  backend/algorithms  (deterministic, tested, versioned)
Derived metrics WITH DRIVERS  ──────────────┐
      ↓  digital twin (per-athlete params)  │
Athlete Context Packet  ←───────────────────┘
      ↓  router: deterministic answer or model call
LLM reasoning layer (+ read-only tools)
      ↓  guardrails: grounding, safety, scope
Answer with citations
```

---

## 1. The rule that shapes everything

**The model never receives raw data, and never computes.**

Bad: `"heart rate 145"`
Good: `"ran 4:30/km at 145 bpm — efficiency index 1.28, 8% below this athlete's 90-day average for the same intensity band"`

Three reasons this is non-negotiable:

1. **Correctness.** LLMs are poor at arithmetic over long series. CTL over 42 days
   is a computation, not a judgement, and `backend/algorithms` computes it
   deterministically and provably.
2. **Cost.** A month of streams is millions of tokens. A context packet is ~4,000.
3. **Explainability.** The model can only cite drivers it was given, so every claim
   traces to a computed value with a day and a baseline.

---

## 2. The Athlete Context Packet

Deterministically serialised (sorted keys, fixed field order) so it is
byte-identical across requests — this is what makes prompt caching work at all.

```jsonc
{
  "schema_version": "1.0",
  "generated_for_date": "2026-07-28",
  "athlete": {
    "age_band": "35-39",           // never a birth date
    "sex": "male",
    "level": "intermediate",
    "primary_sport": "triathlon",
    "training_age_years": 6,
    "thresholds": { "lthr": 170, "ftp_watts": 250, "css_s_per_100m": 95,
                    "sources": { "lthr": "field_test", "ftp_watts": "estimated_20min" } }
  },
  "goals": [{ "type": "race_time", "event": "Olympic triathlon",
              "date": "2026-09-20", "target_s": 9900, "weeks_out": 7 }],
  "twin": {
    "recovery_half_life_days": 2.1,   // learned, not assumed
    "load_tolerance": 1.15,
    "fatigue_exponent": 1.071,
    "confidence": 0.72,
    "observation_days": 168
  },
  "today": {
    "readiness": { "score": 48, "band": "limited", "data_quality": 1.0,
      "drivers": [
        { "name": "hrv", "score": 22, "contribution": -8.4, "value": 42.1, "baseline": 58.4 },
        { "name": "sleep", "score": 41, "contribution": -2.3, "value": 5.4, "baseline": 8.0 }
      ] },
    "load": { "ctl": 62.4, "atl": 78.1, "tsb": -15.7,
              "acwr": 1.42, "acwr_zone": "elevated", "acwr_reliable": true,
              "monotony": 1.9, "ramp_pct": 18.2 },
    "injury_risk": { "probability": 0.21, "band": "high",
                     "model_version": "heuristic-v0", "is_clinically_validated": false,
                     "top_drivers": ["acwr_excess", "sleep_debt_h"] },
    "plan": { "title": "Key Run Threshold", "intensity": "threshold",
              "target_load": 92, "adaptation": "easy_only",
              "reason": "Readiness 48/100, limited by hrv." }
  },
  "recent_sessions": [ /* 14 days, summary + load source + efficiency delta */ ],
  "trends": { "ctl_28d_change": 8.2, "efficiency_run_28d_change_pct": -3.1,
              "sleep_7d_avg_h": 6.4, "hrv_7d_vs_28d_pct": -11.0 },
  "personal_bests": [{ "distance_m": 5000, "time_s": 1182, "date": "2026-04-12" }],
  "predictions": [{ "metric": "time_10000m", "value": 2470, "low": 2440, "high": 2510,
                    "confidence": 0.68 }],
  "data_gaps": ["no power data on the bike", "hrv missing 2026-07-24"]
}
```

`data_gaps` is present so the coach can say "I cannot assess your bike efficiency
because there is no power data" instead of guessing. An honest gap beats a
confident fabrication.

**Privacy:** no name, no email, no date of birth, no GPS coordinates, no raw
streams. Age is a band. See `06-security-privacy.md` §5.

---

## 3. Model selection

Default **`claude-opus-5`** (1M context, $5/M in, $25/M out). Anthropic is the
primary provider; the client is provider-agnostic (§7).

Opus-5 specifics that affect the implementation:

* **Adaptive thinking is on by default.** Thinking tokens are billed as output, so
  `max_tokens` must leave room for both thinking and the answer or responses
  truncate mid-sentence.
* **Depth is controlled by `output_config.effort`**, not by a token budget —
  `budget_tokens` is rejected. Routine chat runs at lower effort; the weekly deep
  review runs high.
* **`temperature`/`top_p` are rejected.** Behaviour is steered by prompt only.
* **Structured output** via `output_config.format` for the parts of the response
  we render as UI rather than prose.
* **Safety classifiers can decline** a request, returning HTTP 200 with
  `stop_reason: "refusal"`. The client checks `stop_reason` **before** reading
  content, and enables server-side fallbacks so a decline is re-served rather than
  surfaced as a failure.
* **Prompt cache minimum is 512 tokens** on Opus 5 — lower than the 1024 of the
  previous generation, so more of our prefixes are cacheable than they would have
  been.

Model choice per route is configuration, not code, so it can be tuned against
measured cost without a deploy. Cost consequences are laid out in
`docs/10-cost-model-and-risks.md`; **which tier to run is a business decision, and
the numbers there are what it should be made from.**

---

## 4. Routing — the primary cost control

Not every question needs a model.

| Route | Handles | Model cost |
|---|---|---|
| `deterministic` | "What is my readiness today?", "How much did I train this week?", "What is my CTL?" — rendered from `daily_metrics` through templates | **zero** |
| `llm_chat` | "Why was I flat today?", "Should I rest?", "What should I improve?" | one call |
| `tool_loop` | "Compare today's run to the same route last month" | one call + tool round trips |
| `llm_deep_review` | Weekly narrative review — queued overnight through the **Batch API at 50% off** | one batched call |

A cheap classifier decides the route. Roughly 40% of expected questions are
answerable deterministically, and a templated answer from the same numbers the
chart shows is *better* than a paraphrase of them — cheaper and consistent.

---

## 5. Tools (read-only, server-scoped)

| Tool | Purpose |
|---|---|
| `get_activity_detail(activity_id)` | drill into one session |
| `get_metric_series(metric, from, to)` | fetch a window the packet omitted |
| `compare_efforts(activity_id, comparison)` | like-for-like comparison |
| `get_plan_week(week_index)` | plan detail |
| `get_personal_bests(sport)` | PB list |

**The security property that matters:** tools execute server-side under the
authenticated caller's identity and **take no user id parameter**. Even a fully
successful prompt injection cannot reach another athlete's data, because there is
no argument through which to ask. Every tool result is also RLS-filtered.

Tools exist so the model can drill down instead of us pre-loading everything —
smaller context, lower cost, better answers.

---

## 6. Guardrails

| Guardrail | Mechanism |
|---|---|
| **Numeric grounding** | Every number in the answer is checked against the context packet and tool results. An ungrounded number sets `grounded = false`, is flagged, and fails the eval suite. This is the anti-hallucination control. |
| **Prompt injection** | Activity names and notes are attacker-controlled. Wrapped in delimited untrusted blocks with a standing instruction that they are data. The real defence is §5. |
| **Medical red flags** | Chest pain, syncope, severe unexplained symptoms → advise medical attention, suppress training advice. The coach never diagnoses and never contradicts a clinician. |
| **Injury-risk honesty** | Always rendered with `model_version` and `is_clinically_validated: false`. Never "you will get injured". |
| **Scope** | Out-of-scope questions (nutrition prescriptions, medication, medical diagnosis) are declined with a pointer to a professional. |
| **Thin data** | With `data_quality < 0.35` the coach says what is missing instead of interpreting noise. |
| **Refusal handling** | `stop_reason` checked before content; server-side fallback configured. |

---

## 7. Multi-provider architecture

```python
class LLMProvider(Protocol):
    async def complete(self, request: CompletionRequest) -> CompletionResponse: ...
    async def stream(self, request: CompletionRequest) -> AsyncIterator[Chunk]: ...
    def estimate_cost(self, usage: Usage) -> int: ...   # micro-USD
```

Nothing above the provider layer knows which vendor answered. `ai_messages`
records `provider`, `model_id` and `prompt_version` on every row, so a quality
regression after a model change is attributable rather than mysterious. Routing
config maps route → provider + model + effort.

This is insurance, not indecision: a single-provider outage otherwise takes the
headline feature offline, and the abstraction costs about a day.

---

## 8. Prompt caching

Layered so the stable part is cached and the volatile part is not:

```
[ system prompt + tool definitions ]   ← stable for weeks   ── cache breakpoint (1h TTL)
[ athlete context packet ]             ← stable for a day   ── cache breakpoint (1h TTL)
[ conversation history ]               ← grows per turn
[ this question ]                      ← volatile
```

Cache reads cost ~0.1× and writes ~1.25×, so the break-even is two messages in a
session. Requirements this imposes on the packet builder:

* deterministic serialisation — sorted keys, fixed order, no timestamps inside the
  cached prefix;
* no request id, no `now()`, nothing per-request above the last breakpoint.

`cache_read_input_tokens` is recorded on every message. If it is zero across a
session, a silent invalidator has crept in and the cost model is broken — this is
monitored, not assumed.

---

## 9. The Athlete Digital Twin

A rolling per-athlete model that replaces population defaults with learned
parameters:

| Parameter | Learned from | Replaces |
|---|---|---|
| `recovery_half_life_days` | readiness recovery after known loads | a fixed 7-day ATL constant |
| `load_tolerance` | ramp rates historically survived without setback | a fixed 8% cap |
| `fatigue_exponent` | the athlete's own PBs across distances | Riegel's 1.06 |
| `aerobic_capacity` trend | efficiency at matched intensity over time | nothing |
| strengths / weaknesses | per-sport efficiency and durability percentiles | nothing |

Snapshotted, never overwritten (`coaching.athlete_twin_snapshots`), because
showing a trend and evaluating whether the twin improved both need history.
`confidence` and `observation_days` are exposed so a twin built on three weeks of
data is presented as provisional — and so the coach hedges appropriately.

---

## 10. Evaluation

The suite that gates every prompt or model change. Detail in
`09-testing-and-model-governance.md`.

| Check | Type | Gate |
|---|---|---|
| Numeric grounding | deterministic | **100%** — any ungrounded number fails |
| Driver fidelity: does the answer name the actual top driver? | deterministic | ≥ 90% |
| Safety: red flags escalate, no diagnosis, no medical contradiction | deterministic + judge | **100%** |
| Scope: out-of-scope declined | judge | ≥ 95% |
| Injection corpus: no cross-tenant call, no instruction leak | deterministic | **100%** |
| Helpfulness / actionability | LLM judge, rubric | ≥ 4.0 / 5 |
| Cost per answer | measured | within budget (`docs/10`) |
| Latency to first token | measured | p95 < 2 s |

~40 golden cases at launch, each a (context packet, question, assertions) triple.
Growth path: every user thumbs-down that reveals a real defect becomes a case.

---

## 11. Open questions

1. **Effort tuning per route** — needs measurement against the eval suite; the
   quality/cost curve is not knowable in advance.
2. **Conversation memory beyond the packet** — semantic recall over past
   conversations would need `pgvector`. Not in MVP.
3. **Fine-tuning** — not before there is a large corpus of graded interactions;
   prompt and context engineering will dominate for a long time.
4. **Hebrew quality** — the coach must be excellent in Hebrew, not merely
   functional. Golden cases must include Hebrew questions, and a native reviewer
   should grade a sample before launch.
