"""Data-quality and trust scoring for one ingested activity. Pure and deterministic.

Two jobs that are usually conflated and must not be:

1. **Data quality** — is this measurement good enough to compute on? A heart-rate
   strap that dropped out gives a `data_quality` below the threshold at which the
   product refuses to score readiness, which is a *correctness* concern.
2. **Trust** — is this activity a real thing the athlete did? Rewards create an
   incentive to fake training, and `docs/06` §7 requires a `trust_score` before any
   reward pays out. This is an *integrity* concern.

They share the checks but not the consequences. A dropped HR strap is low quality
and fully trusted. A 400 W FTP-equivalent hour from an athlete whose best is 200 W
is high quality and untrusted.

**Sequencing rule this enforces:** data quality precedes rewards (`docs/12` §5).
Paying for workouts before trust scoring exists is paying for fabricated workouts.

Scope: this module holds the checks computable from **one activity plus the
athlete's own bounds**. Cross-activity checks — the same session under two
accounts, a burst of manual entries, one device shared across accounts — need
history and therefore live in `modules/training/service.py`. The split is
deliberate: keeping these pure is what makes them exhaustively testable.

`MODEL_VERSION` is recorded on every flag. A threshold change must be attributable,
or "why was this held in March?" is unanswerable once the numbers have moved.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from backend.integrations.types import NormalisedActivity

__all__ = [
    "MODEL_VERSION",
    "TRUST_HOLD_THRESHOLD",
    "AthleteBounds",
    "QualityFlag",
    "QualityReport",
    "assess_activity",
]

MODEL_VERSION = "quality-v1"

# Below this, a reward event is held for manual review and **no ledger entry is
# written** (`docs/17` §2.5). Chosen so that a single soft anomaly (0.15) does not
# hold an activity, but any hard implausibility (0.5) does. Tuned against fixtures,
# and it must be re-tuned against real data before rewards go live.
TRUST_HOLD_THRESHOLD = 0.6

Severity = Literal["info", "warning", "critical"]


@dataclass(frozen=True, slots=True)
class QualityFlag:
    """One detected anomaly, with what it cost and why.

    `trust_penalty` and `quality_penalty` are separate because the same observation
    affects the two differently — see the module docstring.
    """

    code: str
    severity: Severity
    detail: str
    trust_penalty: float = 0.0
    quality_penalty: float = 0.0
    observed: float | None = None
    expected: float | None = None


@dataclass(frozen=True, slots=True)
class AthleteBounds:
    """What is physiologically plausible *for this athlete*.

    Supplied by the training module from the profile and the athlete's own history,
    not hardcoded: a world-class cyclist's plausible power is another athlete's
    impossible one, and a single global ceiling would either flag the elite or admit
    the fraudulent. `None` means "we do not know yet", and an unknown bound never
    produces a flag — a new athlete with no history must not be treated as suspect.
    """

    max_plausible_speed_m_s: float | None = None
    max_plausible_power_w: float | None = None
    hr_max: int | None = None
    # Best distance the athlete has ever covered in one session, times a headroom
    # factor. A genuine breakthrough is maybe 20% beyond; 3x is a data error.
    max_plausible_distance_m: float | None = None
    max_plausible_duration_s: float | None = None


@dataclass(frozen=True, slots=True)
class QualityReport:
    """The verdict on one activity.

    `data_quality` gates whether analytics will compute on it; `trust_score` gates
    whether it may earn. Both are clamped to [0, 1], and both carry their flags so
    the decision is explainable to the athlete and auditable by us.
    """

    data_quality: float
    trust_score: float
    flags: tuple[QualityFlag, ...] = ()
    model_version: str = MODEL_VERSION

    @property
    def should_hold_rewards(self) -> bool:
        return self.trust_score < TRUST_HOLD_THRESHOLD

    @property
    def has_critical(self) -> bool:
        return any(f.severity == "critical" for f in self.flags)

    def flag_codes(self) -> tuple[str, ...]:
        return tuple(f.code for f in self.flags)


# Absolute physical ceilings, used only when an athlete-specific bound is unknown.
# These are "no human, ever" limits rather than "unlikely for this athlete" limits,
# deliberately: their job is to reject sensor nonsense and unit-conversion bugs, not
# to judge performance. Sources are world-record pace with generous headroom.
_ABSOLUTE_MAX_SPEED_M_S = {
    "run": 12.5,  # ~2:13/km — faster than any human over any distance
    "bike": 33.0,  # ~119 km/h — descending, but not sustained
    "swim": 3.0,  # ~33 s/100 m
    "multisport": 33.0,
    "strength": 5.0,
    "other": 33.0,
}
_ABSOLUTE_MAX_POWER_W = 2500.0  # a trained sprinter's few-second peak
_ABSOLUTE_MAX_DURATION_S = 60 * 60 * 30.0  # 30 h: longer than any single session


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def assess_activity(
    activity: NormalisedActivity, bounds: AthleteBounds | None = None
) -> QualityReport:
    """Score one normalised activity for data quality and trust.

    Deterministic: the same activity and bounds always give the same report, which is
    what lets a held reward be re-evaluated and lets a threshold change be replayed
    against history.
    """
    bounds = bounds or AthleteBounds()
    flags: list[QualityFlag] = []

    _check_duration(activity, bounds, flags)
    _check_speed(activity, bounds, flags)
    _check_power(activity, bounds, flags)
    _check_heart_rate(activity, bounds, flags)
    _check_internal_consistency(activity, flags)
    _check_provenance(activity, flags)

    data_quality = _clamp(1.0 - sum(f.quality_penalty for f in flags))
    trust = _clamp(1.0 - sum(f.trust_penalty for f in flags))
    return QualityReport(
        data_quality=data_quality,
        trust_score=trust,
        flags=tuple(flags),
    )


def _check_duration(
    activity: NormalisedActivity, bounds: AthleteBounds, flags: list[QualityFlag]
) -> None:
    duration = activity.duration_s
    if duration <= 0:
        flags.append(
            QualityFlag(
                code="duration_non_positive",
                severity="critical",
                detail="Activity has no positive duration; it cannot be a session.",
                quality_penalty=1.0,
                trust_penalty=1.0,
                observed=duration,
            )
        )
        return

    if duration > _ABSOLUTE_MAX_DURATION_S:
        flags.append(
            QualityFlag(
                code="duration_implausible",
                severity="critical",
                detail="Duration exceeds any plausible single session.",
                quality_penalty=0.5,
                trust_penalty=0.6,
                observed=duration,
                expected=_ABSOLUTE_MAX_DURATION_S,
            )
        )
    elif bounds.max_plausible_duration_s and duration > bounds.max_plausible_duration_s:
        flags.append(
            QualityFlag(
                code="duration_beyond_history",
                severity="warning",
                detail="Far longer than anything in this athlete's history.",
                trust_penalty=0.3,
                observed=duration,
                expected=bounds.max_plausible_duration_s,
            )
        )

    # A very short session is not fraud, but it is too little signal to compute a
    # meaningful efficiency or load figure from.
    if duration < 60:
        flags.append(
            QualityFlag(
                code="duration_too_short_to_analyse",
                severity="info",
                detail="Under a minute: too short for meaningful analysis.",
                quality_penalty=0.4,
                observed=duration,
            )
        )


def _check_speed(
    activity: NormalisedActivity, bounds: AthleteBounds, flags: list[QualityFlag]
) -> None:
    # Prefer the derived speed over the reported one: distance and duration are the
    # two values a spoofer must both falsify consistently, and a mismatch between
    # the reported average and the derived one is itself a signal (checked below).
    derived = None
    if activity.distance_m and activity.duration_s > 0:
        derived = activity.distance_m / activity.duration_s
    speed = derived if derived is not None else activity.avg_speed_m_s
    if speed is None:
        return

    ceiling = _ABSOLUTE_MAX_SPEED_M_S.get(activity.sport, 33.0)
    if speed > ceiling:
        flags.append(
            QualityFlag(
                code="speed_physically_impossible",
                severity="critical",
                detail=f"Average speed exceeds the human limit for {activity.sport}.",
                quality_penalty=0.6,
                trust_penalty=1.0,
                observed=speed,
                expected=ceiling,
            )
        )
    elif bounds.max_plausible_speed_m_s and speed > bounds.max_plausible_speed_m_s:
        flags.append(
            QualityFlag(
                code="speed_beyond_athlete_history",
                severity="warning",
                detail="Faster than this athlete has ever sustained.",
                trust_penalty=0.5,
                observed=speed,
                expected=bounds.max_plausible_speed_m_s,
            )
        )


def _check_power(
    activity: NormalisedActivity, bounds: AthleteBounds, flags: list[QualityFlag]
) -> None:
    power = activity.avg_power_w
    if power is None:
        return

    if power > _ABSOLUTE_MAX_POWER_W:
        flags.append(
            QualityFlag(
                code="power_physically_impossible",
                severity="critical",
                detail="Average power exceeds any human sustained output.",
                quality_penalty=0.6,
                trust_penalty=1.0,
                observed=power,
                expected=_ABSOLUTE_MAX_POWER_W,
            )
        )
    elif bounds.max_plausible_power_w and power > bounds.max_plausible_power_w:
        flags.append(
            QualityFlag(
                code="power_beyond_athlete_history",
                severity="warning",
                detail="Higher average power than this athlete has produced before.",
                trust_penalty=0.5,
                observed=power,
                expected=bounds.max_plausible_power_w,
            )
        )

    # Normalised power below average power is arithmetically impossible: NP is a
    # 30-second-rolling fourth-root-mean-fourth-power, which is >= the mean for any
    # non-constant signal and equal only for a perfectly constant one.
    if activity.normalised_power_w is not None and activity.normalised_power_w < power * 0.98:
        flags.append(
            QualityFlag(
                code="normalised_power_below_average",
                severity="warning",
                detail="Normalised power below average power is not arithmetically possible.",
                quality_penalty=0.3,
                observed=activity.normalised_power_w,
                expected=power,
            )
        )


def _check_heart_rate(
    activity: NormalisedActivity, bounds: AthleteBounds, flags: list[QualityFlag]
) -> None:
    avg, peak = activity.avg_hr, activity.max_hr

    if avg is not None and peak is not None and avg > peak:
        flags.append(
            QualityFlag(
                code="hr_average_exceeds_max",
                severity="warning",
                detail="Average heart rate above the reported maximum: sensor fault.",
                quality_penalty=0.4,
                observed=avg,
                expected=peak,
            )
        )

    ceiling = bounds.hr_max
    if ceiling and peak is not None and peak > ceiling * 1.08:
        # 8% headroom: a profile HRmax is often an estimate, and a genuine new
        # maximum in a hard session is normal. Well beyond it is a strap artefact.
        flags.append(
            QualityFlag(
                code="hr_above_known_max",
                severity="info",
                detail="Peak heart rate well above this athlete's known maximum.",
                quality_penalty=0.2,
                observed=peak,
                expected=float(ceiling),
            )
        )

    # A flatline during a claimed hard effort is the signature of a strap that
    # dropped to a cadence-locked reading, and also of a fabricated file.
    #
    # Requires `peak >= avg`: an inverted pair is already reported above as
    # `hr_average_exceeds_max`, and its spread is negative, which would satisfy this
    # predicate and charge a second penalty — including a *trust* penalty — for what
    # is one sensor fault. One root cause must produce one flag, or a broken strap
    # starts looking like a dishonest athlete.
    if (
        avg is not None
        and peak is not None
        and peak >= avg
        and activity.duration_s > 900
        and peak - avg < 2.0
        and activity.sport in ("run", "bike", "swim")
    ):
        flags.append(
            QualityFlag(
                code="hr_flatline",
                severity="warning",
                detail="Heart rate shows almost no variation across a long session.",
                quality_penalty=0.5,
                trust_penalty=0.3,
                observed=peak - avg,
            )
        )


def _check_internal_consistency(activity: NormalisedActivity, flags: list[QualityFlag]) -> None:
    # Reported average speed versus the speed implied by distance and duration.
    if activity.avg_speed_m_s and activity.distance_m and activity.duration_s > 0:
        derived = activity.distance_m / activity.duration_s
        if derived > 0 and abs(activity.avg_speed_m_s - derived) / derived > 0.25:
            flags.append(
                QualityFlag(
                    code="distance_duration_speed_mismatch",
                    severity="warning",
                    detail="Reported average speed disagrees with distance over time.",
                    quality_penalty=0.3,
                    trust_penalty=0.4,
                    observed=activity.avg_speed_m_s,
                    expected=derived,
                )
            )

    # Moving time cannot exceed elapsed time.
    if activity.moving_duration_s and activity.moving_duration_s > activity.duration_s * 1.01:
        flags.append(
            QualityFlag(
                code="moving_time_exceeds_elapsed",
                severity="warning",
                detail="Moving time longer than elapsed time.",
                quality_penalty=0.3,
                observed=activity.moving_duration_s,
                expected=activity.duration_s,
            )
        )

    # Lap durations should roughly account for the session.
    if activity.laps:
        lap_total = sum(lap.duration_s for lap in activity.laps)
        if lap_total > activity.duration_s * 1.05:
            flags.append(
                QualityFlag(
                    code="lap_total_exceeds_duration",
                    severity="info",
                    detail="Laps sum to more than the activity duration.",
                    quality_penalty=0.15,
                    observed=lap_total,
                    expected=activity.duration_s,
                )
            )

    # Elevation gain is bounded by what is climbable in the distance covered — a
    # 45% average grade is not a route, it is a barometric sensor drifting.
    if (
        activity.elevation_gain_m
        and activity.distance_m
        and activity.distance_m > 0
        and activity.elevation_gain_m / activity.distance_m > 0.45
    ):
        flags.append(
            QualityFlag(
                code="elevation_gain_implausible",
                severity="info",
                detail="Elevation gain implies an implausible average gradient.",
                quality_penalty=0.2,
                observed=activity.elevation_gain_m,
            )
        )


def _check_provenance(activity: NormalisedActivity, flags: list[QualityFlag]) -> None:
    """Provenance is not an accusation, but it is a trust input.

    A manual entry is a self-report with no sensor behind it. It is perfectly valid
    training data and the athlete should be able to record it — but `docs/06` §7
    requires manual activities to earn at a reduced rate and to be barred from
    winning sponsored challenges, so the trust score has to reflect the difference.
    """
    if activity.is_manual:
        flags.append(
            QualityFlag(
                code="manually_entered",
                severity="info",
                detail="Self-reported with no device data behind it.",
                trust_penalty=0.35,
                quality_penalty=0.25,
            )
        )

    has_sensor = any(
        v is not None
        for v in (activity.avg_hr, activity.avg_power_w, activity.avg_speed_m_s, activity.distance_m)
    )
    if not has_sensor and not activity.is_manual:
        flags.append(
            QualityFlag(
                code="no_sensor_channels",
                severity="warning",
                detail="Device activity carrying no measurements at all.",
                quality_penalty=0.5,
                trust_penalty=0.2,
            )
        )
