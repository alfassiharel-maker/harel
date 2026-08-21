"""Unit and quantity arithmetic."""

from __future__ import annotations

import unittest
from fractions import Fraction

from backend.squeeze.units import Dimension, Quantity, UnknownUnitError, unit_dimension


class TestUnits(unittest.TestCase):
    def test_binary_and_decimal_prefixes_differ(self) -> None:
        # A policy that confused these would understate an object-storage bill
        # by 7%, which is the whole reason both are in the table.
        self.assertEqual(Quantity.parse(Fraction(1), "GiB").bytes_exact(), 1_073_741_824)
        self.assertEqual(Quantity.parse(Fraction(1), "GB").bytes_exact(), 1_000_000_000)

    def test_decimal_literal_is_exact(self) -> None:
        # 1.2 KiB is 1228.8 bytes; a float would land on 1228.7999999999999 and
        # the ceil below would still be 1229, but the totals would drift.
        quantity = Quantity.parse(Fraction("1.2"), "KiB")
        self.assertEqual(quantity.as_fraction(), Fraction(6144, 5))
        self.assertEqual(quantity.bytes_exact(), 1229)

    def test_bytes_round_up(self) -> None:
        self.assertEqual(Quantity.parse(Fraction("0.5"), "bytes").bytes_exact(), 1)

    def test_month_is_thirty_days_exactly(self) -> None:
        self.assertEqual(
            Quantity.parse(Fraction(1), "month").seconds(),
            Quantity.parse(Fraction(30), "days").seconds(),
        )

    def test_dimensions_do_not_mix(self) -> None:
        with self.assertRaises(TypeError):
            Quantity.parse(Fraction(30), "days").bytes_exact()
        with self.assertRaises(TypeError):
            Quantity.parse(Fraction(30), "GiB").seconds()

    def test_unknown_unit_is_rejected(self) -> None:
        with self.assertRaises(UnknownUnitError):
            Quantity.parse(Fraction(1), "furlong")
        self.assertIs(unit_dimension("MiB"), Dimension.BYTES)

    def test_str_echoes_the_authors_unit(self) -> None:
        self.assertEqual(str(Quantity.parse(Fraction(30), "days")), "30 days")
        self.assertEqual(str(Quantity.parse(Fraction(6), "TiB")), "6 TiB")


if __name__ == "__main__":
    unittest.main()
