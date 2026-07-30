"""Mock provider tests.

Two things under test, and the first matters more than it looks:

**Determinism.** `CLAUDE.md` forbids randomness in fixtures because a flaky
physiology test is worse than no test. This adapter is the fixture behind the whole
ingest → analytics → coach chain, so if it is not byte-stable, every downstream test
inherits the flakiness and nobody will trust a failure.

**Structure.** The synthetic athlete has deliberate shape — a build/recovery cycle, a
bad-sleep week, a missing-HRV day, a rest day, and power on the bike but not the run.
Those are the analytics engine's edge cases, so they need to be present in the
default fixture rather than hand-built per test.
"""

from __future__ import annotations

import datetime as dt
import json
import unittest

from backend.integrations.base import ProviderAdapter
from backend.integrations.mock.adapter import (
    SYNTHETIC_EPOCH,
    MockAdapter,
    synthesise_activity,
    synthesise_wellness,
)
from backend.integrations.quality import assess_activity
from backend.integrations.types import FetchTask, ProviderTokens, RawPayload

ATHLETE = "athlete-1"
# SYNTHETIC_EPOCH is 2026-01-01, a Thursday. Offsets below are chosen from that.
A_MONDAY = dt.date(2026, 1, 5)
A_FRIDAY = dt.date(2026, 1, 9)
A_SUNDAY = dt.date(2026, 1, 11)


class ProtocolConformanceTests(unittest.TestCase):
    def test_satisfies_the_provider_adapter_protocol(self) -> None:
        # It must be a real adapter, not a stub — otherwise it is a mock that lies and
        # the pipeline it validates is not the pipeline that ships.
        self.assertIsInstance(MockAdapter(), ProviderAdapter)

    def test_declares_its_capabilities(self) -> None:
        adapter = MockAdapter()
        self.assertIn("activity_summary", adapter.capabilities)
        self.assertIn("hrv", adapter.capabilities)


class DeterminismTests(unittest.TestCase):
    def test_the_same_athlete_and_day_always_produce_identical_bytes(self) -> None:
        first = synthesise_activity(ATHLETE, A_MONDAY)
        second = synthesise_activity(ATHLETE, A_MONDAY)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))

    def test_wellness_is_equally_stable(self) -> None:
        first = synthesise_wellness(ATHLETE, A_MONDAY)
        second = synthesise_wellness(ATHLETE, A_MONDAY)
        self.assertEqual(first, second)

    def test_different_athletes_differ_on_the_same_day(self) -> None:
        # Otherwise a multi-athlete isolation test would compare identical data and
        # pass even if the isolation were broken.
        mine = synthesise_activity("athlete-1", A_MONDAY)
        theirs = synthesise_activity("athlete-2", A_MONDAY)
        assert mine and theirs
        self.assertNotEqual(mine["summaryId"], theirs["summaryId"])
        self.assertNotEqual(mine["durationInSeconds"], theirs["durationInSeconds"])

    def test_generation_order_does_not_affect_values(self) -> None:
        # A seeded PRNG would fail this: its output depends on call order, so two
        # tests requesting the same day in a different sequence would disagree.
        forward = [synthesise_activity(ATHLETE, A_MONDAY + dt.timedelta(days=d)) for d in range(4)]
        backward = [
            synthesise_activity(ATHLETE, A_MONDAY + dt.timedelta(days=d)) for d in reversed(range(4))
        ]
        self.assertEqual(forward, list(reversed(backward)))


class SyntheticStructureTests(unittest.TestCase):
    def test_friday_is_a_rest_day(self) -> None:
        # The engine must see gaps: a calendar-dense fixture hides the ACWR
        # history-counting behaviour documented in docs/15 §9.
        self.assertEqual(A_FRIDAY.weekday(), 4)
        self.assertIsNone(synthesise_activity(ATHLETE, A_FRIDAY))

    def test_sunday_is_the_long_session(self) -> None:
        sunday = synthesise_activity(ATHLETE, A_SUNDAY)
        monday = synthesise_activity(ATHLETE, A_MONDAY)
        assert sunday and monday
        self.assertGreater(sunday["durationInSeconds"], monday["durationInSeconds"])

    def test_the_bike_has_power_and_the_run_does_not(self) -> None:
        # Exercises the per-sport load precedence table: bike uses power, run falls
        # back to pace. A fixture with power everywhere would never test the fallback.
        bike = synthesise_activity(ATHLETE, A_SUNDAY)
        run = synthesise_activity(ATHLETE, A_MONDAY)
        assert bike and run
        self.assertIn("averagePowerInWatts", bike)
        self.assertNotIn("averagePowerInWatts", run)

    def test_a_recovery_week_is_lighter_than_the_build_weeks(self) -> None:
        # Gives CTL/ATL/TSB and the ramp cap a realistic shape instead of a flat line.
        build = synthesise_activity(ATHLETE, SYNTHETIC_EPOCH + dt.timedelta(days=14 + 4))
        recovery = synthesise_activity(ATHLETE, SYNTHETIC_EPOCH + dt.timedelta(days=21 + 4))
        assert build and recovery
        self.assertLess(recovery["durationInSeconds"], build["durationInSeconds"])

    def test_there_is_a_day_with_no_wellness_at_all(self) -> None:
        # The watch was not worn. Readiness must refuse rather than invent a score.
        self.assertIsNone(synthesise_wellness(ATHLETE, SYNTHETIC_EPOCH))

    def test_the_bad_sleep_week_has_lower_hrv_and_sleep(self) -> None:
        # Readiness should drop for an explainable reason rather than by an injected
        # constant, so the drivers can be asserted downstream.
        good = synthesise_wellness(ATHLETE, SYNTHETIC_EPOCH + dt.timedelta(days=10))
        bad = synthesise_wellness(ATHLETE, SYNTHETIC_EPOCH + dt.timedelta(days=42))
        assert good and bad
        self.assertLess(bad["lastNightAvg"], good["lastNightAvg"])
        self.assertLess(bad["sleepTimeInSeconds"], good["sleepTimeInSeconds"])

    def test_interval_sessions_carry_laps(self) -> None:
        thursday = SYNTHETIC_EPOCH + dt.timedelta(days=7)  # a Thursday: intervals
        session = synthesise_activity(ATHLETE, thursday)
        assert session
        self.assertEqual(len(session["laps"]), 6)


class WebhookTests(unittest.TestCase):
    def test_a_correctly_signed_webhook_verifies(self) -> None:
        # Unlike Garmin, the mock signs — so the pipeline's `signed_valid` branch has
        # a provider to exercise it and a verification regression is caught here.
        adapter = MockAdapter()
        body = b'{"activities":[{"userId":"athlete-1","summaryId":"s-1"}]}'
        self.assertEqual(
            adapter.verify_webhook(headers={"x-mock-signature": adapter.sign(body)}, body=body),
            "signed_valid",
        )

    def test_a_tampered_body_fails_verification(self) -> None:
        adapter = MockAdapter()
        body = b'{"activities":[{"userId":"athlete-1"}]}'
        signature = adapter.sign(body)
        self.assertEqual(
            adapter.verify_webhook(headers={"x-mock-signature": signature}, body=body + b" "),
            "signature_invalid",
        )

    def test_subjects_are_parsed(self) -> None:
        body = b'{"activities":[{"userId":"athlete-1","summaryId":"s-1"}]}'
        subjects = MockAdapter().subjects(body=body)
        self.assertEqual(len(subjects), 1)
        self.assertEqual(subjects[0].provider_user_id, "athlete-1")


class FetchAndNormaliseTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_then_normalise_round_trips(self) -> None:
        adapter = MockAdapter()
        tokens = ProviderTokens(access_token="a", provider_user_id=ATHLETE)
        task = FetchTask(
            kind="activity_summary",
            provider_user_id=ATHLETE,
            window_start=dt.datetime.combine(A_MONDAY, dt.time.min, tzinfo=dt.UTC),
        )
        raw = await adapter.fetch(task=task, tokens=tokens)
        activity = adapter.normalise_activity(raw)

        assert activity is not None
        self.assertEqual(activity.provider, "mock")
        self.assertEqual(activity.provider_user_id, ATHLETE)
        self.assertEqual(activity.sport, "run")
        self.assertEqual(activity.local_date, A_MONDAY)
        self.assertGreater(activity.duration_s, 0)

    async def test_a_rest_day_normalises_to_none(self) -> None:
        # An empty payload must not become a zero-filled session.
        adapter = MockAdapter()
        task = FetchTask(
            kind="activity_summary",
            provider_user_id=ATHLETE,
            window_start=dt.datetime.combine(A_FRIDAY, dt.time.min, tzinfo=dt.UTC),
        )
        raw = await adapter.fetch(task=task, tokens=ProviderTokens(access_token="a"))
        self.assertIsNone(adapter.normalise_activity(raw))

    async def test_wellness_fetch_normalises(self) -> None:
        adapter = MockAdapter()
        task = FetchTask(
            kind="dailies",
            provider_user_id=ATHLETE,
            window_start=dt.datetime.combine(A_MONDAY, dt.time.min, tzinfo=dt.UTC),
        )
        raw = await adapter.fetch(task=task, tokens=ProviderTokens(access_token="a"))
        days = adapter.normalise_wellness(raw)
        self.assertEqual(len(days), 1)
        self.assertEqual(days[0].local_date, A_MONDAY)

    async def test_exchange_rejects_a_state_mismatch(self) -> None:
        adapter = MockAdapter()
        challenge = adapter.authorize(redirect_uri="https://x/cb", scopes=["activity"])
        with self.assertRaises(ValueError):
            await adapter.exchange(params={"state": "wrong", "code": "c"}, challenge=challenge)


class SyntheticDataPassesQualityTests(unittest.TestCase):
    """The fixture must be *clean* — otherwise every downstream test starts degraded."""

    def test_a_synthetic_week_produces_no_quality_flags(self) -> None:
        adapter = MockAdapter()
        checked = 0
        for offset in range(14):
            day = A_MONDAY + dt.timedelta(days=offset)
            document = synthesise_activity(ATHLETE, day)
            if document is None:
                continue
            raw = RawPayload(
                provider="mock",
                kind="activity_summary",
                body=json.dumps(document).encode(),
                provider_user_id=ATHLETE,
            )
            activity = adapter.normalise_activity(raw)
            assert activity is not None, f"failed to normalise {day}"
            report = assess_activity(activity)
            self.assertEqual(
                report.flags, (), f"synthetic activity on {day} was flagged: {report.flag_codes()}"
            )
            checked += 1
        # Guards against the loop silently checking nothing.
        self.assertEqual(checked, 12)

    def test_synthetic_activities_are_fully_trusted(self) -> None:
        adapter = MockAdapter()
        raw = RawPayload(
            provider="mock",
            kind="activity_summary",
            body=json.dumps(synthesise_activity(ATHLETE, A_SUNDAY)).encode(),
            provider_user_id=ATHLETE,
        )
        activity = adapter.normalise_activity(raw)
        assert activity is not None
        self.assertEqual(assess_activity(activity).trust_score, 1.0)


class StreamTests(unittest.TestCase):
    def test_extracts_a_deterministic_stream(self) -> None:
        adapter = MockAdapter()
        raw = RawPayload(
            provider="mock",
            kind="activity_details",
            body=json.dumps(synthesise_activity(ATHLETE, A_MONDAY)).encode(),
            provider_user_id=ATHLETE,
        )
        first = adapter.extract_stream(raw)
        second = adapter.extract_stream(raw)
        assert first is not None and second is not None
        self.assertEqual(first.checksum_sha256, second.checksum_sha256)
        self.assertEqual(first.channels, ("hr",))
        self.assertGreater(first.sample_count, 0)


class BackfillTests(unittest.TestCase):
    def test_produces_one_task_per_day(self) -> None:
        tasks = MockAdapter().backfill_tasks(since=dt.date(2026, 1, 1), until=dt.date(2026, 1, 8))
        self.assertEqual(len(tasks), 7)
        self.assertTrue(all(t.priority == "backfill" for t in tasks))

    def test_inverted_window_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            MockAdapter().backfill_tasks(since=dt.date(2026, 2, 1), until=dt.date(2026, 1, 1))


if __name__ == "__main__":
    unittest.main()
