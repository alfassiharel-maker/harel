"""Data-quality and trust-scoring tests.

Per `CLAUDE.md`, every algorithm needs the anchor case, the undefined case, and one
hand-computed value. The hand-computed cases here are the penalty arithmetic, since
that is what decides whether an athlete's reward is held — and a threshold that
drifts without anyone noticing is how a rewards programme starts paying for
fabricated workouts.

The distinction under test throughout: a dropped heart-rate strap is **low quality,
fully trusted**. An impossible speed is **high quality, untrusted**. Conflating the
two either refuses to analyse honest sessions or pays out on faked ones.
"""

from __future__ import annotations

import datetime as dt
import unittest

from backend.integrations.quality import (
    MODEL_VERSION,
    TRUST_HOLD_THRESHOLD,
    AthleteBounds,
    assess_activity,
)
from backend.integrations.types import NormalisedActivity, NormalisedLap

START = dt.datetime(2026, 7, 20, 6, 30, tzinfo=dt.UTC)


def activity(**overrides: object) -> NormalisedActivity:
    """A clean, unremarkable 10 km run in 45 minutes. The anchor case."""
    base: dict[str, object] = {
        "provider": "mock",
        "provider_activity_id": "a-1",
        "provider_user_id": "u-1",
        "sport": "run",
        "start_time": START,
        "local_date": dt.date(2026, 7, 20),
        "duration_s": 2700.0,
        "distance_m": 10_000.0,
        "avg_speed_m_s": 10_000.0 / 2700.0,
        "avg_hr": 148.0,
        "max_hr": 172.0,
    }
    base.update(overrides)
    return NormalisedActivity(**base)  # type: ignore[arg-type]


class AnchorCaseTests(unittest.TestCase):
    def test_a_clean_activity_is_fully_trusted_and_fully_usable(self) -> None:
        report = assess_activity(activity())
        self.assertEqual(report.data_quality, 1.0)
        self.assertEqual(report.trust_score, 1.0)
        self.assertEqual(report.flags, ())
        self.assertFalse(report.should_hold_rewards)

    def test_model_version_is_recorded(self) -> None:
        # A threshold change must be attributable, or "why was this held in March?"
        # becomes unanswerable once the numbers have moved.
        self.assertEqual(assess_activity(activity()).model_version, MODEL_VERSION)

    def test_unknown_bounds_never_produce_a_flag(self) -> None:
        # A brand-new athlete has no history. Treating that as suspect would hold
        # every first activity on the platform.
        report = assess_activity(activity(), AthleteBounds())
        self.assertEqual(report.flags, ())
        self.assertEqual(report.trust_score, 1.0)


class UndefinedCaseTests(unittest.TestCase):
    def test_zero_duration_is_critical_and_untrusted(self) -> None:
        report = assess_activity(activity(duration_s=0.0))
        self.assertIn("duration_non_positive", report.flag_codes())
        self.assertEqual(report.data_quality, 0.0)
        self.assertEqual(report.trust_score, 0.0)
        self.assertTrue(report.has_critical)

    def test_a_device_activity_with_no_measurements_is_flagged(self) -> None:
        report = assess_activity(
            activity(distance_m=None, avg_speed_m_s=None, avg_hr=None, max_hr=None)
        )
        self.assertIn("no_sensor_channels", report.flag_codes())

    def test_scores_never_leave_the_unit_interval(self) -> None:
        # Several critical flags stack; the clamp keeps the contract that both
        # scores are probabilities-in-[0,1] rather than going negative.
        report = assess_activity(
            activity(duration_s=0.0, avg_power_w=99_999.0, avg_speed_m_s=500.0, distance_m=10_000_000.0)
        )
        self.assertGreaterEqual(report.trust_score, 0.0)
        self.assertGreaterEqual(report.data_quality, 0.0)
        self.assertLessEqual(report.trust_score, 1.0)


class PhysicalImpossibilityTests(unittest.TestCase):
    def test_impossible_run_speed_destroys_trust(self) -> None:
        # 10 km in 300 s is 33.3 m/s — roughly 2.7x the world record pace.
        report = assess_activity(activity(duration_s=300.0, avg_speed_m_s=33.3))
        self.assertIn("speed_physically_impossible", report.flag_codes())
        self.assertEqual(report.trust_score, 0.0)
        self.assertTrue(report.should_hold_rewards)

    def test_the_speed_ceiling_is_per_sport(self) -> None:
        # 20 m/s (72 km/h) is impossible running and unremarkable descending.
        fast = {"duration_s": 3600.0, "distance_m": 72_000.0, "avg_speed_m_s": 20.0}
        self.assertIn("speed_physically_impossible", assess_activity(activity(sport="run", **fast)).flag_codes())
        self.assertNotIn("speed_physically_impossible", assess_activity(activity(sport="bike", **fast)).flag_codes())

    def test_impossible_power_destroys_trust(self) -> None:
        report = assess_activity(activity(sport="bike", avg_power_w=3000.0))
        self.assertIn("power_physically_impossible", report.flag_codes())
        self.assertEqual(report.trust_score, 0.0)

    def test_derived_speed_is_preferred_over_the_reported_one(self) -> None:
        # A spoofer must falsify distance and duration consistently. Here the
        # reported average is plausible but distance/time is not, and the check must
        # catch the pair rather than believing the convenient field.
        report = assess_activity(activity(duration_s=300.0, distance_m=10_000.0, avg_speed_m_s=4.0))
        self.assertIn("speed_physically_impossible", report.flag_codes())


class AthleteRelativeTests(unittest.TestCase):
    def test_speed_beyond_personal_history_reduces_trust_without_zeroing_it(self) -> None:
        # A genuine breakthrough is possible, so this is a warning, not a verdict.
        bounds = AthleteBounds(max_plausible_speed_m_s=4.0)
        # avg_speed is overridden to stay consistent with distance/duration —
        # otherwise the internal-consistency check fires too and the trust penalty
        # under test is no longer the only one contributing.
        report = assess_activity(
            activity(duration_s=2000.0, distance_m=10_000.0, avg_speed_m_s=5.0), bounds
        )
        self.assertIn("speed_beyond_athlete_history", report.flag_codes())
        self.assertAlmostEqual(report.trust_score, 0.5)
        self.assertTrue(report.should_hold_rewards)

    def test_an_elite_athletes_real_effort_is_not_flagged(self) -> None:
        # The reason bounds are per-athlete: a global ceiling would either flag the
        # elite or admit the fraudulent. 10 km in 1800 s is 5.56 m/s — a 3:00/km
        # pace, world-class but real.
        bounds = AthleteBounds(max_plausible_speed_m_s=6.5, max_plausible_power_w=450.0)
        report = assess_activity(
            activity(duration_s=1800.0, distance_m=10_000.0, avg_speed_m_s=10_000.0 / 1800.0),
            bounds,
        )
        self.assertEqual(report.flags, ())

    def test_power_beyond_history_is_a_warning(self) -> None:
        bounds = AthleteBounds(max_plausible_power_w=280.0)
        report = assess_activity(activity(sport="bike", avg_power_w=400.0), bounds)
        self.assertIn("power_beyond_athlete_history", report.flag_codes())


class SensorFaultTests(unittest.TestCase):
    def test_average_hr_above_max_is_a_quality_problem_not_a_trust_problem(self) -> None:
        # The core distinction: a broken strap is not a dishonest athlete.
        report = assess_activity(activity(avg_hr=175.0, max_hr=170.0))
        self.assertIn("hr_average_exceeds_max", report.flag_codes())
        self.assertEqual(report.trust_score, 1.0)
        self.assertLess(report.data_quality, 1.0)

    def test_hr_flatline_across_a_long_session_is_flagged(self) -> None:
        report = assess_activity(activity(duration_s=3600.0, avg_hr=150.0, max_hr=151.0))
        self.assertIn("hr_flatline", report.flag_codes())

    def test_short_session_flatline_is_not_flagged(self) -> None:
        # A 10-minute steady effort legitimately has little HR spread.
        report = assess_activity(activity(duration_s=600.0, distance_m=2000.0, avg_hr=150.0, max_hr=151.0))
        self.assertNotIn("hr_flatline", report.flag_codes())

    def test_hr_above_known_max_allows_eight_percent_headroom(self) -> None:
        bounds = AthleteBounds(hr_max=180)
        # 190 is within 8% of 180 (194.4) — a genuine new max in a hard session.
        self.assertNotIn("hr_above_known_max", assess_activity(activity(max_hr=190.0), bounds).flag_codes())
        # 210 is a strap artefact.
        self.assertIn("hr_above_known_max", assess_activity(activity(max_hr=210.0), bounds).flag_codes())

    def test_normalised_power_below_average_is_arithmetically_impossible(self) -> None:
        # NP is a fourth-root-mean-fourth-power over 30 s windows, so it is >= the
        # mean for any non-constant signal.
        report = assess_activity(activity(sport="bike", avg_power_w=250.0, normalised_power_w=200.0))
        self.assertIn("normalised_power_below_average", report.flag_codes())

    def test_normalised_power_equal_to_average_is_accepted(self) -> None:
        # Equality is legitimate for a perfectly constant effort (a trainer session).
        report = assess_activity(activity(sport="bike", avg_power_w=250.0, normalised_power_w=250.0))
        self.assertNotIn("normalised_power_below_average", report.flag_codes())


class InternalConsistencyTests(unittest.TestCase):
    def test_reported_speed_disagreeing_with_distance_over_time_is_flagged(self) -> None:
        # 10 km in 2700 s is 3.70 m/s; a reported 6.0 m/s is 62% out.
        report = assess_activity(activity(avg_speed_m_s=6.0))
        self.assertIn("distance_duration_speed_mismatch", report.flag_codes())

    def test_small_disagreement_is_tolerated(self) -> None:
        # Providers compute averages over moving time; a few percent is normal.
        report = assess_activity(activity(avg_speed_m_s=3.8))
        self.assertNotIn("distance_duration_speed_mismatch", report.flag_codes())

    def test_moving_time_longer_than_elapsed_is_flagged(self) -> None:
        report = assess_activity(activity(moving_duration_s=3000.0))
        self.assertIn("moving_time_exceeds_elapsed", report.flag_codes())

    def test_laps_summing_beyond_the_activity_are_flagged(self) -> None:
        laps = tuple(NormalisedLap(lap_index=i + 1, duration_s=1000.0) for i in range(4))
        report = assess_activity(activity(laps=laps))
        self.assertIn("lap_total_exceeds_duration", report.flag_codes())

    def test_consistent_laps_are_not_flagged(self) -> None:
        laps = tuple(NormalisedLap(lap_index=i + 1, duration_s=675.0) for i in range(4))
        report = assess_activity(activity(laps=laps))
        self.assertNotIn("lap_total_exceeds_duration", report.flag_codes())

    def test_implausible_gradient_is_flagged(self) -> None:
        # 5000 m of climbing in 10 km is a 50% average gradient: a drifting barometer.
        report = assess_activity(activity(elevation_gain_m=5000.0))
        self.assertIn("elevation_gain_implausible", report.flag_codes())

    def test_a_hard_hill_session_is_not_flagged(self) -> None:
        # 400 m over 10 km is 4% — a genuinely hilly route.
        report = assess_activity(activity(elevation_gain_m=400.0))
        self.assertNotIn("elevation_gain_implausible", report.flag_codes())


class ProvenanceTests(unittest.TestCase):
    def test_manual_entry_reduces_trust_but_stays_valid_training_data(self) -> None:
        # `docs/06` §7: manual activities earn at a reduced rate and cannot win
        # sponsored challenges — but they are legitimate training the athlete did.
        report = assess_activity(activity(is_manual=True))
        self.assertIn("manually_entered", report.flag_codes())
        self.assertAlmostEqual(report.trust_score, 0.65)
        self.assertAlmostEqual(report.data_quality, 0.75)

    def test_manual_entry_alone_does_not_hold_rewards(self) -> None:
        # Deliberate, and the threshold is tuned for it: manual entry scores 0.65
        # against a 0.60 hold threshold, so a self-reported session still earns — at
        # a reduced rate, and barred from sponsored challenges (`docs/06` §7). Holding
        # every manual entry would punish athletes who train without a watch.
        #
        # Asserted explicitly so that moving TRUST_HOLD_THRESHOLD above 0.65 fails
        # here rather than silently freezing every manual activity in production.
        report = assess_activity(activity(is_manual=True))
        self.assertGreater(report.trust_score, TRUST_HOLD_THRESHOLD)
        self.assertFalse(report.should_hold_rewards)

    def test_manual_entry_plus_one_anomaly_does_hold_rewards(self) -> None:
        # The threshold's real job: manual alone is fine, manual plus a second
        # signal is not. 1 - 0.35 - 0.4 = 0.25.
        report = assess_activity(activity(is_manual=True, avg_speed_m_s=6.0))
        self.assertTrue(report.should_hold_rewards)


class HandComputedPenaltyTests(unittest.TestCase):
    def test_penalties_sum_as_documented(self) -> None:
        # Hand-computed, per the CLAUDE.md requirement.
        # Two flags fire on this activity:
        #   hr_average_exceeds_max  → quality 0.4, trust 0.0
        #   moving_time_exceeds_elapsed → quality 0.3, trust 0.0
        # quality = 1 - (0.4 + 0.3) = 0.3 ; trust = 1 - 0 = 1.0
        report = assess_activity(activity(avg_hr=175.0, max_hr=170.0, moving_duration_s=3000.0))
        self.assertEqual(
            sorted(report.flag_codes()),
            ["hr_average_exceeds_max", "moving_time_exceeds_elapsed"],
        )
        self.assertAlmostEqual(report.data_quality, 0.3)
        self.assertAlmostEqual(report.trust_score, 1.0)

    def test_flags_carry_observed_and_expected_for_explainability(self) -> None:
        # Every composite score returns its drivers (CLAUDE.md). A held reward must
        # be explainable to the athlete without reading the code.
        report = assess_activity(activity(avg_hr=175.0, max_hr=170.0))
        flag = next(f for f in report.flags if f.code == "hr_average_exceeds_max")
        self.assertEqual(flag.observed, 175.0)
        self.assertEqual(flag.expected, 170.0)
        self.assertTrue(flag.detail)


class DeterminismTests(unittest.TestCase):
    def test_the_same_activity_always_scores_identically(self) -> None:
        # What lets a held reward be re-evaluated and a threshold change be replayed
        # against history.
        subject = activity(is_manual=True, avg_hr=175.0, max_hr=170.0)
        first, second = assess_activity(subject), assess_activity(subject)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
