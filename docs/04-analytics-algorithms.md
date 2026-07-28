# 04 — Analytics Algorithms

**Status:** ✅ implemented and tested (`backend/algorithms/`, 209 tests)

Every formula below, its source, and — where it matters — the honest limits of the
evidence behind it. Where the literature is contested we say so rather than
presenting a number as settled science.

---

## 0. Design rules

1. **One scale.** Every load score is normalised so that **one hour at threshold =
   100**, whatever the input. Without this a triathlete's power-based bike number
   and pace-based swim number cannot be summed, and every downstream metric
   inherits the inconsistency.
2. **Personal baselines only.** Never a population norm. HRV varies several-fold
   between individuals; comparing an athlete to a population mean produces
   confident nonsense.
3. **Missing ≠ zero.** An absent input renormalises the model's weights and lowers
   `data_quality`. Substituting zero reads as "terrible" instead of "unmeasured".
4. **Every score carries its explanation.** Composite results return ordered
   drivers whose contributions reconstruct the score exactly.
5. **Fail loudly on thin data.** Below `data_quality` 0.35 the API returns 422
   rather than a plausible-looking number.
6. **Standard library only.** No NumPy in this layer — pure functions, trivially
   testable, portable, no I/O. Vectorised paths belong in the batch layer (ADR-004).

---

## 1. Zones — `zones.py`

| Anchor | Model | Bands |
|---|---|---|
| LTHR (preferred) | Friel 5-zone | Z1 <85%, Z2 85–90, Z3 90–95, Z4 95–100, Z5 ≥100% |
| HRmax (fallback) | %HRmax 5-zone | 50–60, 60–70, 70–80, 80–90, ≥90% |
| FTP | Coggan 7-zone | ≤55, 56–75, 76–90, 91–105, 106–120, 121–150, >150% |
| Threshold pace | 5-zone on speed fraction | converted to s/km, ordering inverts |

HRmax falls back to **Tanaka: 208 − 0.7 × age**, not 220 − age, which
underestimates by up to 10 bpm in masters athletes — a large share of this market.

Anchoring on LTHR matters: two athletes with identical HRmax can have thresholds
15 bpm apart, so a %HRmax-only model puts one of them in the wrong zone for most
of a session. `GET /v1/me/zones` therefore returns the anchor used, so an athlete
whose zones came from an *estimated* HRmax can see that.

**Intensity distribution** collapses zones to low (Z1–2) / moderate (Z3) / high
(Z4+). `is_polarised` requires ≥75% low and high ≥ moderate — the shape a
polarised block should have, which also catches the common "always medium-hard"
failure pattern.

---

## 2. Training load — `training_load.py`

### Per-session scores

| Source | Formula | Notes |
|---|---|---|
| **TRIMP** (HR) | `min × HRr × a × e^(b·HRr)`, HRr = (HR−rest)/(max−rest) | Banister. Male a=0.64 b=1.92; female a=0.86 b=1.67. For unspecified sex we **average both forms** rather than defaulting to male, which would misreport load for roughly half the user base by up to ~10% at high intensity. |
| **hrTSS** | `100 × TRIMP(session) / TRIMP(1 h at this athlete's LTHR)` | Self-calibrating per athlete, which is what makes it comparable to a power TSS for the same person. |
| **TSS** (power) | `100 × hours × IF²`, IF = NP/FTP | Coggan. |
| **NP** | 30 s rolling mean → 4th-power mean → 4th root | Undefined below 30 s of samples; returns `None` rather than a wrong number. |
| **rTSS** (run pace) | `100 × hours × IF²`, IF = threshold_pace/actual_pace | Uses **raw** pace: Garmin's summary exposes no grade-adjusted pace, so this under-reports on hills. Hence HR ranks above pace for running. |
| **sTSS** (swim) | `100 × hours × IF³`, IF = CSS/actual | Cubic because drag rises with roughly the square of velocity, so metabolic cost climbs more steeply in water than on land. |
| **sRPE** | `min × RPE × 100/420` | Foster. Calibrated so 60 min at RPE 7 = 100. |

### Source precedence, per sport

| Sport | Order | Why |
|---|---|---|
| Bike | power → HR → RPE | Power is a direct mechanical measurement. |
| Run | running power → **HR** → pace → RPE | HR outranks pace because raw pace has no grade adjustment. |
| Swim | **pace (CSS)** → HR → RPE | Wrist optical HR is unreliable in water. |
| Strength / other | HR → RPE | |

**Cycling FTP is never applied to running power.** They are different quantities
in the same unit, held in separate columns, with a regression test guarding it.

Every score returns `source` and `confidence` (power 0.95 → RPE 0.45), and both
travel to the UI and the AI layer. An unscoreable session returns
`LoadSource.NONE` with score 0 — surfaced as "needs a perceived effort", not as a
zero-effort session.

### Load over time

* **CTL / ATL** — Banister impulse-response EWMA, 42 and 7 days,
  `α = 1 − e^(−1/τ)`. Deliberately *not* the finance `2/(N+1)` convention; the two
  disagree enough to shift a form estimate several points.
* **TSB** = CTL − ATL.
* Rest days are expanded to explicit zeros, so a two-week layoff decays fitness
  for fourteen days rather than two.
* **ACWR** — acute 7-day vs chronic 28-day *daily averages*. Rolling and EWMA
  variants. The EWMA form seeds both averages with the window mean; seeded at
  zero the slow average is only ~63% converged after 28 samples while the fast one
  is ~98%, which inflates the ratio to ~1.55 on a perfectly constant load. That
  bug was caught by a test asserting constant load → ratio 1.0.
  > **Evidence caveat.** ACWR is contested — Impellizzeri and colleagues have shown
  > the coupled rolling form is mathematically prone to spurious association. We
  > compute it because it is a useful, widely understood monotonicity check on ramp
  > rate, and we surface it as **one driver among several**, never as a verdict. It
  > is never the sole input to injury risk. `is_reliable` is false below 28 days of
  > history.
* **Monotony / strain** — Foster. Monotony = mean/SD of daily load; strain =
  weekly load × monotony. A flat week has zero dispersion, so monotony is
  **undefined and returns `None`** rather than a misleading number.
* **Ramp** — week-over-week percentage. Returns `None` from a zero baseline: coming
  back from nothing is a restart, not an infinite ramp.

---

## 3. Readiness — `recovery.py`

Composite 0–100 with weights, renormalised over whatever is present:

| Component | Weight | Method |
|---|---|---|
| HRV | 0.30 | z-score of **ln(rMSSD)** vs a 28-day personal baseline. Log because HRV is log-normal; a raw mean over-weights high outliers. |
| Resting HR | 0.15 | inverted z-score vs the same window |
| Sleep | 0.25 | duration vs need, efficiency, and deep+REM share of total (target 45%) |
| Training load | 0.20 | TSB freshness, ACWR safety, monotony penalty |
| Subjective | 0.10 | Hooper index (soreness, mood, stress, fatigue) |

z-scores map to 0–100 saturating at ±2 SD: an athlete 3 SD below baseline and one
5 SD below need the same advice, so widening the scale adds noise, not information.

**Smallest worthwhile change.** A day-to-day HRV move smaller than the athlete's
own SWC (= 0.5 × within-athlete CV, per Plews) is reported as **exactly neutral**.
Without this gate the score jitters daily on noise and the athlete stops trusting
it — the fastest way to lose a user's confidence in a recovery metric.

**Explainability.** Driver contributions sum **exactly** to `score − 50` — a
tested invariant, because the AI layer's explanation must match the number the
athlete sees.

Bands: <25 compromised · <50 limited · <70 moderate · <85 good · ≥85 prime.

---

## 4. Efficiency — `efficiency.py`

| Sport | Metric | Formula |
|---|---|---|
| Run | Efficiency index | speed (m/min) / avg HR |
| Bike | Efficiency factor | NP / avg HR |
| Swim | Stroke index | velocity × distance-per-stroke |
| Swim | SWOLF | length time + strokes per length (**lower is better**) |
| Any | Aerobic decoupling | `100 × (r₁ − r₂) / r₁`, r = output/HR per half |
| Gym | e1RM | Epley **and** Brzycki, plus their mean |

Every metric is reported as an absolute value **and** a signed percentage delta
against the athlete's own rolling baseline, with the sign normalised so **positive
always means better** — including for SWOLF, where the raw delta is inverted. That
is what produces the sentence the spec asks for: *"8% less efficient than your
average."*

e1RM returns both formulas because they diverge by several percent and disagree in
opposite directions at high and low rep counts; presenting one alone overstates
precision. Above 12 reps neither is trustworthy and `reliable` says so.

Decoupling above ~5% over a steady effort indicates insufficient aerobic
durability for the duration attempted.

---

## 5. Injury risk — `injury_risk.py`

**`model_version = "heuristic-v0"`, `is_clinically_validated = False`, and the API
contract requires both to be rendered.** This is a transparent, literature-informed
risk *index* that flags known risk patterns. It is not fitted, not validated, and
not clinical. It cannot be fitted until we have labelled injury outcomes from real
users — which is why injury reporting ships (Phase 3.4) before any trained model
(3.5), and why a trained model ships only if it beats this baseline on held-out
data.

Logistic model over ten features, each expressed in capped "risk units":

| Feature | Coefficient |
|---|---|
| prior injury in 12 months | **+0.85** |
| low chronic load (CTL below 30) | +0.55 |
| ACWR above 1.30 | +0.45 |
| sleep debt (h/night, 7 d) | +0.35 |
| weekly ramp above 10% | +0.30 |
| HRV suppression days (14 d) | +0.30 |
| monotony above 1.8 | +0.25 |
| resting-HR elevation | +0.20 |
| age over 40 | +0.15 |
| training age (protective) | −0.40 |
| intercept | −2.60 |

Intercept −2.60 gives a ~7% four-week base rate for an athlete with no risk
patterns, compounding to roughly 60% per year — consistent with reported
running-injury incidence. Prior injury is the largest single term because it is
the most consistently replicated predictor in the literature.

Features are capped at 3 risk units so one extreme input cannot saturate the
model, and driver contributions are **log-odds** — the only decomposition that is
arithmetically additive, verified by a test that reconstructs the probability from
the drivers alone.

`extract_features()` / `predict()` are a deliberate two-stage seam: a trained
scikit-learn model replaces `predict` without touching feature extraction, so the
features logged from day one are exactly the features the future model trains on.

---

## 6. Performance prediction — `performance.py`

Three independent families, so a prediction can be cross-checked rather than
trusted:

| Model | Formula |
|---|---|
| **Riegel** | `T₂ = T₁ × (D₂/D₁)^k`, k = 1.06 default, **fitted per athlete** from ≥3 PBs by log-log regression, clamped to 1.00–1.20 |
| **Daniels VDOT** | VO₂ = −4.60 + 0.182258v + 0.000104v²; %VO₂max = 0.8 + 0.1894393e^(−0.012778t) + 0.2989558e^(−0.1932605t); inverted by bisection for a target distance |
| **Critical speed / power** | two-parameter hyperbolic: `P = CP + W′/t` |
| FTP | 0.95 × 20-minute mean power |

`equivalent_times()` returns **both** VDOT and Riegel with the spread as the
interval. Agreement within 2% is high confidence; a 10% divergence means the
athlete has a real distance-specific strength or weakness worth naming — that
disagreement is information, not noise to average away.

Validated against Daniels' published tables: a 20:00 5 km gives VDOT ≈ 49.8, and
the model round-trips back to 1200 s within a second.

**Progression forecast** is a recency-weighted OLS fit (60-day half-life) with a
95% prediction interval that widens honestly with extrapolation distance. Fewer
than three observations returns `None` — two points define a line with zero
residual scatter, and a zero-width interval would be a lie.

**"Will I break my PB next month?"** is answered as a probability from that
interval via the normal CDF. A wide interval yields a probability near 0.5 rather
than false precision.

Clamping the fitted Riegel exponent to 1.00–1.20 matters: a fit outside that range
means the inputs are inconsistent (a fresh 5 km against a stale marathon), not
that the athlete has exotic physiology.

---

## 7. Plan generation and adaptation — `plan.py`

**Generation.** Weekly load starts from the athlete's *measured* CTL × 7 — the
weekly load that sustains their present fitness — floored by level. Starting from
measured load is what stops the plan prescribing a 500-load week to someone
currently training 150.

* Ramp caps: beginner 5%, intermediate 8%, advanced 10% per week.
* Recovery week every 4th at 65%.
* Periodisation base → build → peak → taper, scaled to the weeks available; no
  race date means no taper, because there is nothing to peak for.
* Taper at 70% then 50% of peak.
* Session duration is derived from target load through the **same IF²
  relationship** the load scorer uses, so a compliant session records the load the
  plan predicted. Ad-hoc durations would make the ramp targets fiction.
* Sessions spread proportionally across the week (`round(slot × 7 / count)`), not
  by integer division — which would stack four sessions on four consecutive days.

The ramp cap governs the *progression* of loading weeks. The rebound out of a
recovery week is intentionally larger and is tested separately: it must land one
ramp step above the last loading week, never compound the drop into a spike.

**Daily adaptation** gates the planned session on this morning's readiness:

| Readiness | Action |
|---|---|
| ≥70 | as planned |
| 50–69 | key session down one intensity band, or −20% volume |
| 25–49 | aerobic only, −40% duration |
| <25 | rest |
| `data_quality` < 0.35 | **as planned**, with the reason stating why |

Very-high injury risk caps intensity even on a green readiness day — the two
signals answer different questions, and load pattern can be dangerous while
overnight recovery looks fine.

The thin-data case is deliberate: silently downgrading a session because a watch
failed to sync destroys trust in every later recommendation.

---

## 8. Test coverage

209 tests, all passing, `python3 -m unittest discover -s tests -t .` with no
dependencies installed. Notable properties asserted rather than assumed:

* Every load source anchors to exactly 100 at one threshold hour, and all four
  agree within 0.01 for the same athlete.
* Readiness driver contributions sum to `score − 50`.
* Injury-risk drivers reconstruct the probability.
* Constant load → ACWR exactly 1.0 (the test that caught the EWMA seeding bug).
* Cycling FTP is never applied to running power.
* Plan ramp never exceeds the level cap across loading weeks.
* Undefined statistics return `None`, never a plausible fake (flat-baseline
  z-score, flat-week monotony, ramp from zero, sub-30-second NP, two-point
  forecast).
