from __future__ import annotations

import unittest
from datetime import date, timedelta

from backend.algorithms import plan as planning
from backend.algorithms.types import (
    Adaptation,
    Goal,
    Level,
    RiskBand,
    RiskResult,
    SessionPlan,
    Sport,
    TrainingPhase,
)

from ._fixtures import profile, readiness_result

START = date(2026, 7, 6)


def request(**overrides) -> planning.PlanRequest:
    defaults = dict(
        goal=Goal.RACE_TIME,
        primary_sport=Sport.RUN,
        start_date=START,
        weeks=12,
        sessions_per_week=5,
        level=Level.INTERMEDIATE,
        current_ctl=40.0,
        race_date=START + timedelta(weeks=12),
    )
    defaults.update(overrides)
    return planning.PlanRequest(**defaults)


class TestPlanRequestValidation(unittest.TestCase):
    def test_rejects_impossible_weeks_and_session_counts(self) -> None:
        with self.assertRaises(ValueError):
            request(weeks=0)
        with self.assertRaises(ValueError):
            request(sessions_per_week=0)
        with self.assertRaises(ValueError):
            request(sessions_per_week=20)


class TestPlanStructure(unittest.TestCase):
    def test_the_plan_has_the_requested_shape(self) -> None:
        result = planning.generate_plan(request(), profile())
        self.assertEqual(len(result.weeks), 12)
        for week in result.weeks:
            with self.subTest(week=week.week_index):
                self.assertEqual(len(week.sessions), 5)

    def test_sessions_are_spread_across_distinct_days(self) -> None:
        result = planning.generate_plan(request(sessions_per_week=5), profile())
        for week in result.weeks:
            offsets = [session.day_offset - week.week_index * 7 for session in week.sessions]
            with self.subTest(week=week.week_index):
                self.assertEqual(len(set(offsets)), len(offsets))
                self.assertTrue(all(0 <= offset <= 6 for offset in offsets))

    def test_every_session_has_a_trainable_duration(self) -> None:
        result = planning.generate_plan(request(), profile())
        for week in result.weeks:
            for session in week.sessions:
                with self.subTest(week=week.week_index, title=session.title):
                    self.assertGreaterEqual(session.duration_min, 20)

    def test_duration_is_derived_from_load_at_the_session_intensity(self) -> None:
        """Plan load and recorded load must agree, or the ramp targets are fiction."""
        result = planning.generate_plan(request(), profile())
        session = result.weeks[0].sessions[0]
        expected = 60.0 * session.target_load / planning.LOAD_PER_HOUR[session.intensity]
        self.assertAlmostEqual(session.duration_min, round(expected), delta=1)

    def test_weekly_target_starts_from_current_chronic_load(self) -> None:
        # CTL 60 -> 420 weekly, which exceeds the intermediate floor of 300.
        result = planning.generate_plan(request(current_ctl=60.0), profile())
        self.assertAlmostEqual(result.weeks[0].target_load, 420.0, delta=1.0)

    def test_an_untrained_athlete_gets_the_level_floor_not_zero(self) -> None:
        result = planning.generate_plan(request(current_ctl=0.0, level=Level.BEGINNER), profile())
        self.assertAlmostEqual(result.weeks[0].target_load, 150.0, delta=1.0)


class TestPeriodisation(unittest.TestCase):
    def test_a_recovery_week_lands_every_fourth_week(self) -> None:
        result = planning.generate_plan(request(), profile())
        self.assertTrue(result.weeks[3].is_recovery_week)
        self.assertTrue(result.weeks[7].is_recovery_week)
        self.assertFalse(result.weeks[0].is_recovery_week)

    def test_a_recovery_week_is_lighter_than_the_week_before_it(self) -> None:
        result = planning.generate_plan(request(), profile())
        self.assertLess(result.weeks[3].target_load, result.weeks[2].target_load)

    def test_the_block_progresses_through_phases_toward_the_race(self) -> None:
        result = planning.generate_plan(request(), profile())
        phases = [week.phase for week in result.weeks]
        self.assertIn(TrainingPhase.BASE, phases)
        self.assertIn(TrainingPhase.BUILD, phases)
        self.assertIn(TrainingPhase.TAPER, phases)
        self.assertIs(result.weeks[-1].phase, TrainingPhase.TAPER)

    def test_the_taper_sheds_load(self) -> None:
        result = planning.generate_plan(request(), profile())
        taper_weeks = [week for week in result.weeks if week.phase is TrainingPhase.TAPER]
        peak_load = max(week.target_load for week in result.weeks if week.phase is not TrainingPhase.TAPER)
        for week in taper_weeks:
            with self.subTest(week=week.week_index):
                self.assertLess(week.target_load, peak_load)
        # The final week must be the lightest of the taper.
        self.assertLess(taper_weeks[-1].target_load, taper_weeks[0].target_load)

    def test_no_race_date_means_no_taper(self) -> None:
        result = planning.generate_plan(request(race_date=None), profile())
        self.assertNotIn(TrainingPhase.TAPER, [week.phase for week in result.weeks])

    def test_the_ramp_cap_is_never_exceeded_across_loading_weeks(self) -> None:
        """The single most important safety property of the generator.

        The cap governs the *progression* — successive loading weeks. A recovery
        week is a deliberate drop and the week after it returns to the
        progression, so comparing every adjacent pair would measure the intended
        rebound rather than the ramp.
        """
        for level in Level:
            with self.subTest(level=level):
                result = planning.generate_plan(request(level=level, weeks=16), profile())
                cap = planning.RAMP_CAP_BY_LEVEL[level]
                loading = [
                    week.target_load
                    for week in result.weeks
                    if not week.is_recovery_week and week.phase is not TrainingPhase.TAPER
                ]
                for previous, current in zip(loading, loading[1:]):
                    increase = 100.0 * (current - previous) / previous
                    # target_load is rounded to 0.1, worth ~0.03pp of noise in
                    # the ratio; the tolerance sits above that and far below any
                    # meaningful ramp violation.
                    self.assertLessEqual(increase, cap + 0.1)

    def test_the_week_after_a_recovery_week_resumes_the_progression(self) -> None:
        """The rebound out of a down week must land one ramp step above the last
        loading week — not compound the drop into a spike."""
        result = planning.generate_plan(request(weeks=16), profile())
        cap = planning.RAMP_CAP_BY_LEVEL[Level.INTERMEDIATE]
        for index, week in enumerate(result.weeks):
            if not week.is_recovery_week or index == 0 or index + 1 >= len(result.weeks):
                continue
            previous_loading = result.weeks[index - 1].target_load
            rebound = result.weeks[index + 1].target_load
            with self.subTest(week=index):
                self.assertLess(week.target_load, previous_loading)
                self.assertLessEqual(
                    100.0 * (rebound - previous_loading) / previous_loading, cap + 0.1
                )

    def test_beginners_ramp_more_slowly_than_advanced_athletes(self) -> None:
        beginner = planning.generate_plan(request(level=Level.BEGINNER), profile())
        advanced = planning.generate_plan(request(level=Level.ADVANCED), profile())
        self.assertLess(beginner.ramp_cap_pct, advanced.ramp_cap_pct)


class TestIntensityMix(unittest.TestCase):
    def test_most_sessions_are_easy(self) -> None:
        result = planning.generate_plan(request(), profile())
        build_week = next(week for week in result.weeks if week.phase is TrainingPhase.BUILD)
        easy = sum(1 for session in build_week.sessions if session.intensity in ("easy", "recovery"))
        self.assertGreaterEqual(easy, len(build_week.sessions) - 2)

    def test_a_recovery_week_contains_no_quality_work(self) -> None:
        result = planning.generate_plan(request(), profile())
        recovery_week = result.weeks[3]
        for session in recovery_week.sessions:
            with self.subTest(title=session.title):
                self.assertEqual(session.intensity, "recovery")

    def test_peak_weeks_introduce_vo2max_work(self) -> None:
        result = planning.generate_plan(request(), profile())
        peak_weeks = [week for week in result.weeks if week.phase is TrainingPhase.PEAK]
        self.assertTrue(peak_weeks)
        intensities = {session.intensity for week in peak_weeks for session in week.sessions}
        self.assertIn("vo2max", intensities)


class TestMultisport(unittest.TestCase):
    def test_a_triathlon_block_interleaves_all_three_sports(self) -> None:
        result = planning.generate_plan(
            request(primary_sport=Sport.RUN, secondary_sports=(Sport.BIKE, Sport.SWIM), sessions_per_week=6),
            profile(),
        )
        sports = {session.sport for week in result.weeks for session in week.sessions}
        self.assertEqual(sports, {Sport.RUN, Sport.BIKE, Sport.SWIM})

    def test_the_primary_sport_keeps_the_majority_of_sessions(self) -> None:
        result = planning.generate_plan(
            request(primary_sport=Sport.RUN, secondary_sports=(Sport.BIKE, Sport.SWIM), sessions_per_week=6),
            profile(),
        )
        sessions = [session for week in result.weeks for session in week.sessions]
        run_share = sum(1 for session in sessions if session.sport is Sport.RUN) / len(sessions)
        self.assertGreaterEqual(run_share, 0.5)

    def test_a_single_sport_plan_stays_single_sport(self) -> None:
        result = planning.generate_plan(request(), profile())
        sports = {session.sport for week in result.weeks for session in week.sessions}
        self.assertEqual(sports, {Sport.RUN})


class TestDailyAdaptation(unittest.TestCase):
    def _session(self, **overrides) -> SessionPlan:
        defaults = dict(
            day_offset=0,
            sport=Sport.RUN,
            title="Key Run Threshold",
            target_load=90.0,
            duration_min=60,
            intensity="threshold",
            is_key_session=True,
        )
        defaults.update(overrides)
        return SessionPlan(**defaults)

    def test_good_readiness_keeps_the_plan(self) -> None:
        result = planning.adapt_today(self._session(), readiness_result(88.0))
        self.assertIs(result.action, Adaptation.AS_PLANNED)
        self.assertEqual(result.session, self._session())

    def test_moderate_readiness_downgrades_a_key_session_by_one_band(self) -> None:
        result = planning.adapt_today(self._session(), readiness_result(60.0))
        self.assertIs(result.action, Adaptation.REDUCE_INTENSITY)
        assert result.session is not None
        self.assertEqual(result.session.intensity, "tempo")
        self.assertLess(result.session.target_load, 90.0)

    def test_moderate_readiness_trims_volume_on_an_easy_session(self) -> None:
        result = planning.adapt_today(self._session(intensity="easy", is_key_session=False), readiness_result(60.0))
        self.assertIs(result.action, Adaptation.REDUCE_VOLUME)
        assert result.session is not None
        self.assertEqual(result.session.duration_min, 48)

    def test_limited_readiness_allows_aerobic_work_only(self) -> None:
        result = planning.adapt_today(self._session(), readiness_result(40.0))
        self.assertIs(result.action, Adaptation.EASY_ONLY)
        assert result.session is not None
        self.assertEqual(result.session.intensity, "easy")
        self.assertFalse(result.session.is_key_session)

    def test_compromised_readiness_prescribes_rest(self) -> None:
        result = planning.adapt_today(self._session(), readiness_result(15.0))
        self.assertIs(result.action, Adaptation.REST)
        self.assertIsNone(result.session)

    def test_thin_data_leaves_the_plan_untouched(self) -> None:
        """Silently downgrading a session because a watch failed to sync would
        destroy trust in every later recommendation."""
        result = planning.adapt_today(self._session(), readiness_result(30.0, data_quality=0.2))
        self.assertIs(result.action, Adaptation.AS_PLANNED)
        self.assertIn("Not enough recovery data", result.reason)

    def test_missing_readiness_leaves_the_plan_untouched(self) -> None:
        result = planning.adapt_today(self._session(), None)
        self.assertIs(result.action, Adaptation.AS_PLANNED)

    def test_no_session_means_rest(self) -> None:
        result = planning.adapt_today(None, readiness_result(90.0))
        self.assertIs(result.action, Adaptation.REST)

    def test_very_high_injury_risk_caps_intensity_despite_green_readiness(self) -> None:
        risk = RiskResult(
            probability=0.45,
            band=RiskBand.VERY_HIGH,
            drivers=(),
            model_version="heuristic-v0",
        )
        result = planning.adapt_today(self._session(), readiness_result(80.0), risk)
        self.assertIs(result.action, Adaptation.REDUCE_INTENSITY)
        assert result.session is not None
        self.assertEqual(result.session.intensity, "tempo")

    def test_high_but_not_extreme_risk_does_not_override_green_readiness(self) -> None:
        risk = RiskResult(
            probability=0.25,
            band=RiskBand.HIGH,
            drivers=(),
            model_version="heuristic-v0",
        )
        result = planning.adapt_today(self._session(), readiness_result(80.0), risk)
        self.assertIs(result.action, Adaptation.AS_PLANNED)

    def test_every_adaptation_carries_a_reason_for_the_athlete(self) -> None:
        for score in (95.0, 60.0, 40.0, 10.0):
            with self.subTest(score=score):
                result = planning.adapt_today(self._session(), readiness_result(score))
                self.assertTrue(result.reason)
                self.assertGreater(len(result.reason), 20)


class TestCompliance(unittest.TestCase):
    def test_compliance_is_a_bounded_ratio(self) -> None:
        self.assertAlmostEqual(planning.weekly_compliance(400.0, 300.0), 0.75)
        self.assertEqual(planning.weekly_compliance(400.0, 2000.0), 2.0)
        self.assertEqual(planning.weekly_compliance(0.0, 300.0), 0.0)


if __name__ == "__main__":
    unittest.main()
