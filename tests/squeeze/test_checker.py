"""Semantic rules. Each test names the rule it pins down."""

from __future__ import annotations

import unittest
from fractions import Fraction

from backend.squeeze import compile_text
from backend.squeeze.diagnostics import Severity
from backend.squeeze.model import CodecKind, Sensitivity

BASE = """version 1
codec delta { kind lossless; ratio 6.0; cpu 0.6 ms per MiB; applies_to timeseries; impl delta_varint }
tier hot { medium memory; latency 3 ms; unit_cost 3400 minor per GiB per month; codecs delta }
tier cold { medium object; latency 2 s; unit_cost 9 minor per GiB per month; codecs delta }
"""


def codes(source: str) -> list[str]:
    _, bag = compile_text(source)
    return [d.code for d in bag.items]


class TestVersion(unittest.TestCase):
    def test_missing_version_is_an_error(self) -> None:
        program, bag = compile_text("codec a { kind lossless }\n")
        self.assertIsNone(program)
        self.assertIn("SQZ0301", [d.code for d in bag.errors])

    def test_future_version_is_refused_not_guessed(self) -> None:
        program, bag = compile_text("version 99\n")
        self.assertIsNone(program)
        self.assertIn("SQZ0302", [d.code for d in bag.errors])


class TestCodecRules(unittest.TestCase):
    def test_lossless_codec_gets_fidelity_one(self) -> None:
        program, bag = compile_text(BASE)
        self.assertIsNotNone(program)
        assert program is not None
        codec = program.codecs["delta"]
        self.assertIs(codec.kind, CodecKind.LOSSLESS)
        self.assertEqual(codec.fidelity, 1)
        self.assertFalse(bag.has_errors())

    def test_lossy_codec_must_declare_fidelity(self) -> None:
        source = "version 1\ncodec q { kind lossy; ratio 4; applies_to tensor; impl int8_quantise }\n"
        self.assertIn("SQZ0313", codes(source))

    def test_lossless_codec_may_not_claim_lost_signal(self) -> None:
        source = "version 1\ncodec q { kind lossless; fidelity 0.9; ratio 4; applies_to tensor }\n"
        self.assertIn("SQZ0314", codes(source))

    def test_ratio_below_one_is_rejected(self) -> None:
        source = "version 1\ncodec q { kind lossless; ratio 0.8; applies_to blob }\n"
        self.assertIn("SQZ0310", codes(source))

    def test_missing_ratio_is_unknown_not_an_error(self) -> None:
        source = "version 1\ncodec q { kind lossless; applies_to blob; impl zlib }\n"
        program, bag = compile_text(source)
        self.assertIsNotNone(program)
        self.assertEqual([d.code for d in bag.unknowns], ["SQZ0311"])

    def test_unmeasurable_declaration_is_flagged(self) -> None:
        # A ratio with no implementation cannot be verified against bytes.
        source = "version 1\ncodec q { kind lossless; ratio 3; applies_to blob }\n"
        self.assertIn("SQZ0357", codes(source))

    def test_cpu_is_normalised_to_per_mib(self) -> None:
        source = "version 1\ncodec q { kind lossless; ratio 3; applies_to blob; cpu 1024 ms per GiB }\n"
        program, _ = compile_text(source)
        assert program is not None
        cpu = program.codecs["q"].cpu_per_mib
        assert cpu is not None
        # 1024 ms per GiB is 1 ms per MiB, held in canonical seconds.
        self.assertEqual(cpu.as_fraction(), Fraction(1, 1000))

    def test_unknown_data_kind_is_rejected(self) -> None:
        source = "version 1\ncodec q { kind lossless; ratio 3; applies_to sparkles }\n"
        self.assertIn("SQZ0316", codes(source))


class TestClassRules(unittest.TestCase):
    def test_class_needs_a_kind(self) -> None:
        self.assertIn("SQZ0323", codes(BASE + "class s { record 24 bytes; retain hot 7 days }\n"))

    def test_retention_into_an_undeclared_tier(self) -> None:
        source = BASE + "class s : timeseries { record 24 bytes; retain glacier 7 days }\n"
        self.assertIn("SQZ0351", codes(source))

    def test_windows_may_not_decrease(self) -> None:
        # `retain hot 30 days, cold 7 days` claims to keep data in cold storage
        # that it has already dropped, so it is an error, not a warning.
        source = BASE + "class s : timeseries { record 24 bytes; retain hot 30 days, cold 7 days }\n"
        self.assertIn("SQZ0353", codes(source))

    def test_a_class_with_no_retention_is_an_error(self) -> None:
        self.assertIn("SQZ0327", codes(BASE + "class s : timeseries { record 24 bytes }\n"))

    def test_missing_growth_is_unknown_not_zero(self) -> None:
        source = BASE + "class s : timeseries { record 24 bytes; retain hot 7 days }\n"
        program, bag = compile_text(source)
        self.assertIsNotNone(program)
        assert program is not None
        self.assertIsNone(program.classes["s"].growth)
        self.assertEqual([d.severity for d in bag.items if d.code == "SQZ0326"], [Severity.UNKNOWN])

    def test_growth_shape_is_enforced(self) -> None:
        source = BASE + "class s : timeseries { record 24 bytes; growth 900 records; retain hot 7 days }\n"
        self.assertIn("SQZ0329", codes(source))

    def test_growth_scope_and_period(self) -> None:
        source = (
            BASE
            + "class s : timeseries { record 24 bytes; growth 900 records per user per month; retain hot 7 days }\n"
        )
        program, bag = compile_text(source)
        assert program is not None
        growth = program.classes["s"].growth
        assert growth is not None
        self.assertEqual((growth.amount, growth.scope), (900, "user"))
        self.assertEqual(growth.period.seconds(), 30 * 86_400)
        self.assertFalse(bag.has_errors())

    def test_sensitivity_defaults_to_routine(self) -> None:
        source = BASE + "class s : timeseries { record 24 bytes; retain hot 7 days }\n"
        program, _ = compile_text(source)
        assert program is not None
        self.assertIs(program.classes["s"].sensitivity, Sensitivity.ROUTINE)

    def test_no_applicable_codec_warns(self) -> None:
        source = BASE + "class docs : document { record 2 KiB; retain hot 7 days }\n"
        self.assertIn("SQZ0354", codes(source))


class TestTierAndPolicyRules(unittest.TestCase):
    def test_tier_listing_an_undeclared_codec(self) -> None:
        source = "version 1\ntier hot { medium memory; codecs nope }\n"
        self.assertIn("SQZ0350", codes(source))

    def test_unit_cost_shape_is_fixed(self) -> None:
        source = "version 1\ntier hot { medium memory; unit_cost 9 minor per month }\n"
        self.assertIn("SQZ0321", codes(source))

    def test_duplicate_declaration(self) -> None:
        source = "version 1\ntier hot { medium memory }\ntier hot { medium disk }\n"
        self.assertIn("SQZ0303", codes(source))

    def test_unknown_setting_names_the_alternatives(self) -> None:
        program, bag = compile_text("version 1\ntier hot { medium memory; ration 3 }\n")
        self.assertIsNone(program)
        error = next(d for d in bag.errors if d.code == "SQZ0305")
        self.assertIn("codecs", error.hint or "")

    def test_policy_needs_an_objective(self) -> None:
        self.assertIn("SQZ0335", codes(BASE + "policy p { population 10 users }\n"))

    def test_policy_covering_an_undeclared_class(self) -> None:
        source = BASE + "policy p { objective minimise bytes; covers ghosts }\n"
        self.assertIn("SQZ0355", codes(source))

    def test_limit_and_requirement_parsing(self) -> None:
        source = (
            BASE
            + "policy p { objective minimise bytes; limit total 6 TiB; require fidelity >= 0.99; require explanation }\n"
        )
        program, bag = compile_text(source)
        assert program is not None
        policy = program.policies["p"]
        self.assertEqual(policy.limits["total"].bytes_exact(), 6 * 1024**4)
        self.assertEqual(
            [(r.subject, r.operator) for r in policy.requirements],
            [("fidelity", ">="), ("explanation", None)],
        )
        self.assertFalse(bag.has_errors())

    def test_duplicate_limit_is_rejected(self) -> None:
        source = BASE + "policy p { objective minimise bytes; limit total 6 TiB; limit total 7 TiB }\n"
        self.assertIn("SQZ0338", codes(source))

    def test_example_policy_compiles_clean(self) -> None:
        from pathlib import Path

        source = Path("policies/footprint.sqz").read_text(encoding="utf-8")
        program, bag = compile_text(source)
        self.assertIsNotNone(program)
        self.assertEqual(bag.errors, [])
        self.assertEqual(bag.warnings, [])


if __name__ == "__main__":
    unittest.main()
