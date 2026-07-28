# 00 — Product Brief

## The problem

A Garmin watch already tells an athlete their heart rate, HRV, VO₂max estimate and
a recovery time. Almost none of them know what to *do* with it. The data is
abundant and the interpretation is absent.

The gap is not more numbers. It is the sentence a good coach says: *"your HRV is
14% below your baseline and you slept five hours — do the easy run, move the
intervals to Thursday."*

## The product

An AI coach that knows one specific athlete's body over time.

```
Watch and sensor data
    ↓
Deterministic analytics (load, recovery, efficiency, risk, prediction)
    ↓
A per-athlete model that learns how this body responds
    ↓
A coach that explains, decides, and adapts today's session
```

Sports: triathlon, running, cycling, swimming, gym, general fitness.
Platforms: iOS, Android, web.

## Who it is for

| Segment | What they want | Why they would pay |
|---|---|---|
| Age-group triathletes | one plan across three sports, and to not blow up before race day | already spend far more than 15 ₪/month on coaching, gear and races |
| Serious recreational runners | a PB, without the injury that usually precedes it | injury is the thing that ends their season |
| Data-curious gym and fitness users | to know whether it is working | currently guessing |
| Coaches (Phase 5) | to serve more athletes with less manual analysis | it is their livelihood |

Primary market: Israel first (Hebrew, ILS, local partners), then international.

## What makes it defensible

Not the LLM — anyone can call one. Three things compound instead:

1. **A deterministic analytics engine that is actually right.** Load normalised to
   one scale across sports, readiness against personal baselines with a
   noise-gated HRV signal, explanations that reconstruct the score exactly. This is
   built and tested.
2. **A per-athlete model that improves with use.** Learned recovery half-life,
   load tolerance and fatigue exponent replace population defaults. Athlete-months
   of data are the moat, and a competitor cannot copy them.
3. **Honesty as a product feature.** Injury risk labelled unvalidated. A refusal to
   score readiness on one night of data. Efficiency stated as a delta against the
   athlete's own history. Athletes who train seriously can tell the difference
   between a real number and a confident guess, and they stop trusting an app that
   produces the latter.

## Business model

15 ₪/month Premium, with a Free tier. Net of VAT and store commission that is
roughly **$2.41/subscriber/month**, which makes AI inference cost the binding
constraint on the whole design — see `docs/10-cost-model-and-risks.md`.

Additional lines: partner commissions, sponsored challenges, a coach marketplace,
individual sensor unlocks, and a higher Premium tier later.

The rewards club is a customer-acquisition mechanism funded by partner
commissions, not a revenue line.

## What we are deliberately not building first

* Nutrition prescriptions, medical advice, diagnosis.
* A social network. Groups and challenges are retention features, not the product.
* Support for every device on day one. Garmin first, behind an adapter interface.
* A trained injury-prediction model before there are labelled injuries to train on.

## How we will know it works

The claim is that athletes get better. That gets measured, with an honest control
group and both the naive and controlled numbers reported — see
`docs/09-testing-and-model-governance.md` §7. Target: **more than half of active
athletes measurably improved at 90 days.**

If that number does not hold, the product does not work, regardless of how good the
retention charts look.
