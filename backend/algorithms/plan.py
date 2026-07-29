"""Training-plan generation and daily adaptation.

Two separate concerns, deliberately kept apart:

  * `generate_plan` builds a periodised block from the athlete's goal, level and
    current chronic load. It is deterministic and cheap — no LLM involved.
  * `adapt_today` gates the planned session against this morning's readiness and
    risk. This is what makes the plan "not a fixed plan" as the spec requires.

Session durations are derived from target load through the same IF^2 relationship
that `training_load` uses, so a plan's predicted load and the load actually
recorded for a compliant session agree. Ad-hoc durations would make the plan's
ramp targets fiction.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import date

from .stats import clamp
from .types import (
    Adaptation,
    AdaptedSession,
    AthleteProfile,
    Goal,
    Level,
    ReadinessResult,
    RiskBand,
    RiskResult,
    SessionPlan,
    Sport,
    TrainingPhase,
    TrainingPlan,
    WeekPlan,
)

__all__ = ["LOAD_PER_HOUR", "RAMP_CAP_BY_LEVEL", "PlanRequest", "adapt_today", "generate_plan"]

# Weekly ramp ceilings. Deliberately conservative for beginners: the ramp rate
# an experienced athlete tolerates is the one that injures a novice.
RAMP_CAP_BY_LEVEL: dict[Level, float] = {
    Level.BEGINNER: 5.0,
    Level.INTERMEDIATE: 8.0,
    Level.ADVANCED: 10.0,
}

# Load accrued per hour at each intensity, from 100 x IF^2 at the representative
# intensity factor for that band.
LOAD_PER_HOUR: dict[str, float] = {
    "recovery": 36.0,  # IF 0.60
    "easy": 49.0,  # IF 0.70
    "tempo": 72.0,  # IF 0.85
    "threshold": 96.0,  # IF 0.98
    "vo2max": 117.0,  # IF 1.08
}

# Minimum weekly load we will plan for, by level, when an athlete has no
# chronic-load history to build from.
_STARTING_WEEKLY_LOAD: dict[Level, float] = {
    Level.BEGINNER: 150.0,
    Level.INTERMEDIATE: 300.0,
    Level.ADVANCED: 500.0,
}

_RECOVERY_WEEK_INTERVAL = 4
_RECOVERY_WEEK_FACTOR = 0.65
_TAPER_FACTORS = (0.70, 0.50)


@dataclass(frozen=True)
class PlanRequest:
    goal: Goal
    primary_sport: Sport
    start_date: date
    weeks: int
    sessions_per_week: int
    level: Level = Level.INTERMEDIATE
    current_ctl: float = 0.0
    secondary_sports: tuple[Sport, ...] = ()
    race_date: date | None = None

    def __post_init__(self) -> None:
        if self.weeks < 1:
            raise ValueError("weeks must be at least 1")
        if not 1 <= self.sessions_per_week <= 14:
            raise ValueError("sessions_per_week must be within 1..14")


def _phase_for_week(week_index: int, total_weeks: int, has_race: bool) -> TrainingPhase:
    """Base -> build -> peak -> taper, scaled to however many weeks exist.

    Without a race date there is nothing to peak for, so the block stays in
    base/build and never tapers.
    """
    if not has_race:
        return TrainingPhase.BASE if week_index < max(1, total_weeks // 3) else TrainingPhase.BUILD

    taper_weeks = 2 if total_weeks >= 8 else (1 if total_weeks >= 4 else 0)
    peak_weeks = 2 if total_weeks >= 12 else (1 if total_weeks >= 6 else 0)
    remaining = total_weeks - taper_weeks - peak_weeks
    base_weeks = max(0, int(round(remaining * 0.45)))

    if week_index < base_weeks:
        return TrainingPhase.BASE
    if week_index < base_weeks + (remaining - base_weeks):
        return TrainingPhase.BUILD
    if week_index < base_weeks + (remaining - base_weeks) + peak_weeks:
        return TrainingPhase.PEAK
    return TrainingPhase.TAPER


def _session_template(phase: TrainingPhase, sessions_per_week: int) -> list[tuple[str, bool, float]]:
    """(intensity, is_key_session, share_of_weekly_load) for one week.

    Shares are normalised by the caller. The intensity mix targets roughly 80%
    of *time* at low intensity, which falls out of giving the quality sessions a
    minority of the load at a much higher load-per-hour.
    """
    if phase is TrainingPhase.RECOVERY:
        return [("recovery", False, 1.0) for _ in range(sessions_per_week)]

    if phase is TrainingPhase.TAPER:
        template: list[tuple[str, bool, float]] = [("threshold", True, 1.2)]
        template += [("easy", False, 1.0) for _ in range(max(0, sessions_per_week - 1))]
        return template

    quality: list[tuple[str, bool, float]]
    if phase is TrainingPhase.BASE:
        quality = [("tempo", True, 1.4)]
    elif phase is TrainingPhase.BUILD:
        quality = [("threshold", True, 1.5), ("tempo", True, 1.3)]
    else:  # PEAK / RACE
        quality = [("vo2max", True, 1.6), ("threshold", True, 1.4)]

    quality = quality[: max(1, sessions_per_week - 1)]
    long_session = [("easy", True, 2.0)] if sessions_per_week >= 3 else []
    easy_count = max(0, sessions_per_week - len(quality) - len(long_session))
    return quality + long_session + [("easy", False, 1.0) for _ in range(easy_count)]


def _rotate_sports(request: PlanRequest, count: int) -> list[Sport]:
    """Spread sessions across sports for multisport athletes.

    The primary sport keeps the majority of sessions; secondaries are
    interleaved, which is what a triathlon block needs rather than three
    independent single-sport plans.
    """
    if not request.secondary_sports:
        return [request.primary_sport] * count
    rotation = [request.primary_sport, *request.secondary_sports]
    out: list[Sport] = []
    for index in range(count):
        # Every other slot goes to the primary sport.
        out.append(
            request.primary_sport
            if index % 2 == 0
            else rotation[1 + (index // 2) % len(request.secondary_sports)]
        )
    return out


def generate_plan(request: PlanRequest, profile: AthleteProfile | None = None) -> TrainingPlan:
    """Build a periodised plan.

    Weekly load starts from the athlete's current chronic load (CTL x 7, i.e.
    the weekly load that sustains their present fitness) and ramps within the
    level's cap, with a recovery week every fourth week and a taper into a race.
    Starting from measured CTL rather than from a template is what prevents the
    plan prescribing a 500-load week to someone currently training 150.
    """
    level = request.level
    ramp_cap = RAMP_CAP_BY_LEVEL[level]
    baseline = max(request.current_ctl * 7.0, _STARTING_WEEKLY_LOAD[level])
    has_race = request.race_date is not None

    weeks: list[WeekPlan] = []
    progressive_load = baseline
    peak_load = baseline

    for week_index in range(request.weeks):
        phase = _phase_for_week(week_index, request.weeks, has_race)
        is_recovery_week = (week_index + 1) % _RECOVERY_WEEK_INTERVAL == 0 and phase in (
            TrainingPhase.BASE,
            TrainingPhase.BUILD,
        )

        if phase is TrainingPhase.TAPER:
            taper_position = _taper_position(week_index, request.weeks, has_race)
            target_load = peak_load * _TAPER_FACTORS[min(taper_position, len(_TAPER_FACTORS) - 1)]
        elif is_recovery_week:
            target_load = progressive_load * _RECOVERY_WEEK_FACTOR
        else:
            if week_index > 0:
                progressive_load *= 1.0 + ramp_cap / 100.0
            target_load = progressive_load
            peak_load = max(peak_load, target_load)

        effective_phase = TrainingPhase.RECOVERY if is_recovery_week else phase
        template = _session_template(effective_phase, request.sessions_per_week)
        sports = _rotate_sports(request, len(template))
        total_share = sum(share for _, _, share in template) or 1.0

        sessions: list[SessionPlan] = []
        for slot, ((intensity, is_key, share), sport) in enumerate(zip(template, sports, strict=False)):
            session_load = target_load * share / total_share
            duration_min = int(round(60.0 * session_load / LOAD_PER_HOUR[intensity]))
            sessions.append(
                SessionPlan(
                    # Spread sessions across the week rather than stacking them
                    # on consecutive days.
                    day_offset=week_index * 7 + _spread_day(slot, len(template)),
                    sport=sport,
                    title=_session_title(intensity, is_key, sport),
                    target_load=round(session_load, 1),
                    duration_min=max(20, duration_min),
                    intensity=intensity,
                    is_key_session=is_key,
                    notes=_session_notes(intensity),
                )
            )

        weeks.append(
            WeekPlan(
                week_index=week_index,
                phase=effective_phase,
                target_load=round(target_load, 1),
                sessions=tuple(sorted(sessions, key=lambda s: s.day_offset)),
                is_recovery_week=is_recovery_week,
            )
        )

    return TrainingPlan(
        weeks=tuple(weeks),
        primary_sport=request.primary_sport,
        goal=request.goal,
        ramp_cap_pct=ramp_cap,
    )


def _taper_position(week_index: int, total_weeks: int, has_race: bool) -> int:
    taper_weeks = 2 if total_weeks >= 8 else (1 if total_weeks >= 4 else 0)
    if taper_weeks == 0:
        return 0
    first_taper_week = total_weeks - taper_weeks
    return max(0, week_index - first_taper_week)


def _spread_day(slot: int, count: int) -> int:
    """Spread `count` sessions evenly across a 7-day week.

    Integer division (7 // count) would put four sessions on four consecutive
    days and leave three rest days stacked at the end; proportional placement
    gives distinct, evenly spaced days for any count up to 7. Above 7 the week
    necessarily contains doubles, and days repeat by design.
    """
    if count <= 0:
        return 0
    if count >= 7:
        return slot % 7
    return min(6, int(round(slot * 7.0 / count)))


def _session_title(intensity: str, is_key: bool, sport: Sport) -> str:
    label = {
        "recovery": "Recovery",
        "easy": "Aerobic",
        "tempo": "Tempo",
        "threshold": "Threshold",
        "vo2max": "VO2max intervals",
    }[intensity]
    prefix = "Key " if is_key and intensity != "easy" else ("Long " if is_key else "")
    return f"{prefix}{sport.value.title()} {label}".strip()


def _session_notes(intensity: str) -> str:
    return {
        "recovery": "Zone 1 only. If it does not feel easy, stop.",
        "easy": "Zone 2. Conversational the whole way.",
        "tempo": "Zone 3 blocks with full warm-up and cool-down.",
        "threshold": "Zone 4 intervals. Hold the pace, do not race it.",
        "vo2max": "Zone 5 intervals with full recoveries between reps.",
    }[intensity]


# --- Daily adaptation -------------------------------------------------------

_MIN_DATA_QUALITY_TO_ADAPT = 0.35

_DOWNGRADE = {
    "vo2max": "threshold",
    "threshold": "tempo",
    "tempo": "easy",
    "easy": "easy",
    "recovery": "recovery",
}


def adapt_today(
    session: SessionPlan | None,
    readiness_result: ReadinessResult | None,
    risk_result: RiskResult | None = None,
) -> AdaptedSession:
    """Gate today's planned session on this morning's readiness and risk.

    Refuses to adapt on thin data: with `data_quality` below 0.35 the plan is
    left alone and the reason says so. Silently downgrading a session because a
    watch failed to sync is worse than leaving it — the athlete loses trust in
    every future recommendation.
    """
    if session is None:
        return AdaptedSession(action=Adaptation.REST, session=None, reason="No session scheduled today.")

    if readiness_result is None or readiness_result.data_quality < _MIN_DATA_QUALITY_TO_ADAPT:
        return AdaptedSession(
            action=Adaptation.AS_PLANNED,
            session=session,
            reason="Not enough recovery data this morning to adjust — keeping the plan as written.",
        )

    score = readiness_result.score
    top_driver = readiness_result.drivers[0].name if readiness_result.drivers else "recovery data"

    if score < 25:
        return AdaptedSession(
            action=Adaptation.REST,
            session=None,
            reason=f"Readiness {score:.0f}/100 ({readiness_result.band.value}), driven by {top_driver}. Full rest today.",
        )

    if score < 50:
        eased = dataclasses.replace(
            session,
            intensity="easy",
            is_key_session=False,
            duration_min=max(20, int(session.duration_min * 0.6)),
            target_load=round(session.target_load * 0.5, 1),
            title=f"Easy {session.sport.value.title()} (adapted)",
            notes=_session_notes("easy"),
        )
        return AdaptedSession(
            action=Adaptation.EASY_ONLY,
            session=eased,
            reason=f"Readiness {score:.0f}/100, limited by {top_driver}. Aerobic only today; the quality work moves.",
        )

    if score < 70:
        if session.is_key_session and session.intensity != "easy":
            downgraded_intensity = _DOWNGRADE[session.intensity]
            adjusted = dataclasses.replace(
                session,
                intensity=downgraded_intensity,
                duration_min=max(20, int(session.duration_min * 0.9)),
                target_load=round(
                    session.target_load
                    * LOAD_PER_HOUR[downgraded_intensity]
                    / LOAD_PER_HOUR[session.intensity],
                    1,
                ),
                notes=_session_notes(downgraded_intensity),
            )
            return AdaptedSession(
                action=Adaptation.REDUCE_INTENSITY,
                session=adjusted,
                reason=f"Readiness {score:.0f}/100, held back by {top_driver}. One intensity band down.",
            )
        adjusted = dataclasses.replace(
            session,
            duration_min=max(20, int(session.duration_min * 0.8)),
            target_load=round(session.target_load * 0.8, 1),
        )
        return AdaptedSession(
            action=Adaptation.REDUCE_VOLUME,
            session=adjusted,
            reason=f"Readiness {score:.0f}/100, held back by {top_driver}. Volume trimmed 20%.",
        )

    if (
        risk_result is not None
        and risk_result.band is RiskBand.VERY_HIGH
        and session.intensity in ("vo2max", "threshold")
    ):
        # Readiness alone says green, but the load pattern says otherwise.
        capped = dataclasses.replace(
            session,
            intensity="tempo",
            duration_min=max(20, int(session.duration_min * 0.85)),
            target_load=round(
                session.target_load * LOAD_PER_HOUR["tempo"] / LOAD_PER_HOUR[session.intensity], 1
            ),
            notes=_session_notes("tempo"),
        )
        return AdaptedSession(
            action=Adaptation.REDUCE_INTENSITY,
            session=capped,
            reason=(
                "Recovery looks fine, but your recent load pattern is in the elevated-risk band "
                "— capping today's intensity as a precaution."
            ),
        )

    return AdaptedSession(
        action=Adaptation.AS_PLANNED,
        session=session,
        reason=f"Readiness {score:.0f}/100 ({readiness_result.band.value}). Go as planned.",
    )


def weekly_compliance(planned_load: float, actual_load: float) -> float:
    """Actual load as a fraction of planned, clamped to 0..2.

    Feeds the next block's ramp: an athlete consistently at 70% compliance
    should have the plan come down to meet them, not keep ramping away from them.
    """
    if planned_load <= 0:
        return 0.0
    return round(clamp(actual_load / planned_load, 0.0, 2.0), 3)
