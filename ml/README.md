# Machine learning

⏳ **Phase 3.** Deliberately not started, for a reason worth stating plainly:

> **There is nothing to train yet.** An injury-prediction model needs labelled
> injuries. Until athletes have reported real injuries in the app (roadmap task
> 3.4), any "ML model" here would be a model fitted to invented labels — which is
> worse than the honest heuristic that ships today, because it would *look*
> validated.

What ships now instead: `backend/algorithms/injury_risk.py`, a transparent
logistic index with literature-informed coefficients, labelled
`is_clinically_validated = False` in the type system and rendered as such in the
API.

```
ml/
├── data/              ⏳ dataset builders (from analytics.prediction_records etc.)
├── features/          ⏳ feature engineering — must reuse injury_risk.extract_features
├── training/          ⏳ training scripts per model, with fixed seeds
├── evaluation/        ⏳ held-out scoring, calibration, comparison vs the baseline
├── experiments/       ⏳ notebooks — exploration only, never a production path
└── artifacts/         gitignored; models are versioned in analytics.algorithm_versions
```

## The seam that already exists

`backend/algorithms/injury_risk.py` is split so a trained model drops in without
touching feature extraction:

```python
extract_features(profile, loads, wellness, ref_day) -> dict[str, float]   # keep
predict(features) -> float                                                # replace
```

Consequence: **the features logged from day one are exactly the features the future
model trains on.** No retrospective backfill, no train/serve skew from two
implementations of the same feature.

## Promotion gate (from `docs/09` §6.5)

A trained model replaces the heuristic only if all hold:

1. The eval suite passes.
2. MAE is not worse on held-out data.
3. Interval calibration is within tolerance — a 95% interval must contain the
   actual value about 95% of the time.
4. No safety regression.
5. A variant experiment shows no engagement harm.

If a trained model does not beat `heuristic-v0`, **the heuristic stays**. Shipping
a model because it is a model, rather than because it is better, is how a product
acquires a feature that is worse than what it replaced.

## Framework note

scikit-learn first: the problems are tabular with modest sample sizes, where
gradient boosting is the strong baseline and a neural network is usually not.
PyTorch is in the stack for when a sequence model over training history
demonstrably beats that baseline — measured, not assumed (ADR-004).
