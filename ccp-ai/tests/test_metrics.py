"""Cost model tests, including hand-computed values.

A projection shown to an investor has to be arithmetic anyone can redo on paper,
so one test does exactly that.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import metrics


class FormattingTests(unittest.TestCase):
    def test_binary_prefixes(self) -> None:
        self.assertEqual(metrics.fmt_bytes(512), "512 B")
        self.assertEqual(metrics.fmt_bytes(1024), "1.00 KiB")
        self.assertEqual(metrics.fmt_bytes(1024**3), "1.00 GiB")

    def test_missing_is_a_dash_not_a_zero(self) -> None:
        self.assertEqual(metrics.fmt_bytes(None), "—")


class SavingsTests(unittest.TestCase):
    def test_ratio_and_factor(self) -> None:
        savings = metrics.StoreSavings(raw_bytes=1000, stored_bytes=250)
        self.assertEqual(savings.saved_bytes, 750)
        self.assertAlmostEqual(savings.savings_ratio, 0.75)
        self.assertAlmostEqual(savings.compression_factor, 4.0)

    def test_empty_store_reports_none_not_zero(self) -> None:
        savings = metrics.StoreSavings(raw_bytes=0, stored_bytes=0)
        self.assertIsNone(savings.savings_ratio)
        self.assertIsNone(savings.compression_factor)
        self.assertIsNone(savings.savings_ratio_vs_gzip)

    def test_gzip_baseline_comparison(self) -> None:
        savings = metrics.StoreSavings(raw_bytes=1000, stored_bytes=200, baseline_gzip_bytes=800)
        self.assertAlmostEqual(savings.savings_ratio_vs_gzip, 0.75)


class ProjectionTests(unittest.TestCase):
    def test_hand_computed_seven_billion_case(self) -> None:
        # 7e9 params x 2 bytes = 14e9 bytes per checkpoint.
        # 10 variants x 100,000 downloads = 1,000,000 downloads.
        # Baseline egress: 14e9 x 1e6 = 1.4e16 bytes = 13,038,516 GiB
        #   x $0.08 = $1,043,081.28 (13,038,516.036 GiB, not the truncated count).
        projection = metrics.project_at_scale(
            measured_variant_ratio=0.10, params_billions=7.0, variants=10, downloads_per_variant=100_000
        )
        payload = projection.as_dict()

        self.assertEqual(projection.model_bytes, 14_000_000_000)
        self.assertEqual(projection.baseline_egress_bytes, 14_000_000_000 * 1_000_000)
        self.assertAlmostEqual(payload["egress"]["baseline_usd"], 1_043_081.28, places=1)

        # CCP egress: base once per client (100,000 clients) plus a 10% delta on
        # every one of the million downloads.
        expected_ccp = 14_000_000_000 * 100_000 + 1_400_000_000 * 1_000_000
        self.assertEqual(projection.ccp_egress_bytes, expected_ccp)

    def test_storage_side_counts_one_base_plus_deltas(self) -> None:
        projection = metrics.project_at_scale(
            measured_variant_ratio=0.25, params_billions=1.0, variants=4, downloads_per_variant=1
        )
        model = 2_000_000_000
        self.assertEqual(projection.baseline_storage_bytes, model * 4)
        self.assertEqual(projection.ccp_storage_bytes, model + int(model * 0.25) * 4)

    def test_savings_ratio_is_below_the_per_variant_ratio(self) -> None:
        # The base still has to reach every client, so fleet savings can never
        # equal the per-variant delta ratio. A projection that claimed otherwise
        # would be the easiest thing in the demo to disprove.
        projection = metrics.project_at_scale(measured_variant_ratio=0.10, variants=10, downloads_per_variant=1000)
        payload = projection.as_dict()
        self.assertLess(payload["savings_ratio"], 0.90)
        self.assertGreater(payload["savings_ratio"], 0.5)

    def test_more_variants_improves_the_ratio(self) -> None:
        low = metrics.project_at_scale(measured_variant_ratio=0.2, variants=2).as_dict()["savings_ratio"]
        high = metrics.project_at_scale(measured_variant_ratio=0.2, variants=50).as_dict()["savings_ratio"]
        self.assertGreater(high, low)

    def test_ratio_of_one_yields_no_saving(self) -> None:
        payload = metrics.project_at_scale(measured_variant_ratio=1.0, variants=5).as_dict()
        self.assertLessEqual(payload["savings_ratio"], 0.0)

    def test_invalid_inputs_refused(self) -> None:
        for kwargs in (
            {"measured_variant_ratio": 0.0},
            {"measured_variant_ratio": 1.5},
            {"measured_variant_ratio": -0.1},
            {"measured_variant_ratio": 0.5, "variants": 0},
            {"measured_variant_ratio": 0.5, "downloads_per_variant": 0},
            {"measured_variant_ratio": 0.5, "params_billions": 0},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                metrics.project_at_scale(**kwargs)

    def test_assumptions_travel_with_the_numbers(self) -> None:
        payload = metrics.project_at_scale(measured_variant_ratio=0.3).as_dict()
        assumptions = payload["assumptions"]
        self.assertEqual(assumptions["egress_usd_per_gib"], metrics.EGRESS_USD_PER_GIB)
        self.assertEqual(assumptions["storage_usd_per_gib_month"], metrics.STORAGE_USD_PER_GIB_MONTH)
        self.assertIn("measured", assumptions["note"])


class UnitPriceTests(unittest.TestCase):
    def test_monthly_storage(self) -> None:
        self.assertAlmostEqual(metrics.monthly_storage_usd(metrics.GIB * 1000), 23.0, places=6)

    def test_egress(self) -> None:
        self.assertAlmostEqual(metrics.egress_usd(metrics.GIB * 1000), 80.0, places=6)


if __name__ == "__main__":
    unittest.main()
