"""Tests for the CCP Core.

Runs with no dependencies installed, from the repository root:

    python3 -m unittest discover -s ccp/tests -t .

The contract under test is: whatever goes in comes back out, byte for byte, and
the representation never costs more than storing the units plainly.
"""

from __future__ import annotations

import hashlib
import unittest

from ccp.capabilities import CCPReader
from ccp.core import (
    Add,
    BuildConfig,
    CCPFormatError,
    CCPIntegrityError,
    ChangeProgram,
    Copy,
    InMemoryUnitSource,
    build_change_program,
    build_model,
    chunk_unit,
    container_overhead,
    deserialize,
    paste,
    serialize,
)
from ccp.core.change_program import (
    merge_adjacent,
    read_uvarint,
    uvarint_size,
    write_uvarint,
)
from ccp.core.chunking import chunk_boundaries
from ccp.core.representation import KIND_DERIVED, KIND_LITERAL


def deterministic_bytes(size: int, seed: int) -> bytes:
    """Reproducible pseudo-random bytes. Never `random`: a flaky fixture would
    make a failure impossible to interpret."""
    out = bytearray()
    counter = 0
    while len(out) < size:
        out += hashlib.blake2b(
            counter.to_bytes(8, "little") + seed.to_bytes(8, "little"), digest_size=64
        ).digest()
        counter += 1
    return bytes(out[:size])


# ---------------------------------------------------------------------------
# varints — hand-computed widths
# ---------------------------------------------------------------------------


class TestVarint(unittest.TestCase):
    def test_hand_computed_widths(self) -> None:
        self.assertEqual(uvarint_size(0), 1)
        self.assertEqual(uvarint_size(127), 1)
        self.assertEqual(uvarint_size(128), 2)
        self.assertEqual(uvarint_size(16383), 2)
        self.assertEqual(uvarint_size(16384), 3)

    def test_round_trip(self) -> None:
        for value in (0, 1, 127, 128, 300, 65535, 1 << 32):
            buffer = bytearray()
            write_uvarint(buffer, value)
            self.assertEqual(len(buffer), uvarint_size(value))
            decoded, pos = read_uvarint(bytes(buffer), 0)
            self.assertEqual(decoded, value)
            self.assertEqual(pos, len(buffer))

    def test_truncated_varint_is_rejected(self) -> None:
        with self.assertRaises(CCPFormatError):
            read_uvarint(b"\x80", 0)

    def test_negative_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            write_uvarint(bytearray(), -1)


# ---------------------------------------------------------------------------
# change programs — the Paste anchor case
# ---------------------------------------------------------------------------


class TestChangeProgram(unittest.TestCase):
    def test_paste_reconstructs(self) -> None:
        base = b"the quick brown fox jumps over the lazy dog"
        # base[0:20] == "the quick brown fox ", base[25:43] == " over the lazy dog"
        program = ChangeProgram((Copy(0, 20), Add(b"WALKS"), Copy(25, 18)))
        self.assertEqual(
            paste(base, program), b"the quick brown fox WALKS over the lazy dog"
        )

    def test_hand_computed_encoded_size(self) -> None:
        # 1 varint for the instruction count (1 byte, count=2)
        # COPY: opcode 1 + varint(0)=1 + varint(20)=1        -> 3
        # ADD:  opcode 1 + varint(3)=1 + 3 payload bytes     -> 5
        program = ChangeProgram((Copy(0, 20), Add(b"abc")))
        self.assertEqual(program.encoded_size(), 1 + 3 + 5)
        self.assertEqual(len(program.encode()), 9)

    def test_encode_decode_round_trip(self) -> None:
        program = ChangeProgram((Copy(5, 11), Add(b"\x00\xff data"), Copy(0, 3)))
        self.assertEqual(ChangeProgram.decode(program.encode()), program)

    def test_output_length_without_execution(self) -> None:
        program = ChangeProgram((Copy(0, 10), Add(b"xyz"), Copy(3, 7)))
        self.assertEqual(program.output_length, 20)
        self.assertEqual(program.copied_bytes, 17)
        self.assertEqual(program.added_bytes, 3)

    def test_empty_program_has_no_reuse_ratio(self) -> None:
        # The undefined case: no bytes, so no meaningful ratio. None, not 0.0.
        self.assertIsNone(ChangeProgram(()).reuse_ratio)

    def test_copy_past_base_is_rejected(self) -> None:
        with self.assertRaises(CCPFormatError):
            paste(b"short", ChangeProgram((Copy(0, 999),)))

    def test_zero_length_instructions_are_invalid(self) -> None:
        with self.assertRaises(ValueError):
            Copy(0, 0)
        with self.assertRaises(ValueError):
            Add(b"")

    def test_unknown_opcode_is_rejected(self) -> None:
        with self.assertRaises(CCPFormatError):
            ChangeProgram.decode(b"\x01\x7f\x00")

    def test_merge_adjacent(self) -> None:
        merged = merge_adjacent([Copy(0, 5), Copy(5, 5), Add(b"a"), Add(b"b")])
        self.assertEqual(merged, (Copy(0, 10), Add(b"ab")))

    def test_merge_does_not_fuse_discontiguous_copies(self) -> None:
        merged = merge_adjacent([Copy(0, 5), Copy(50, 5)])
        self.assertEqual(merged, (Copy(0, 5), Copy(50, 5)))


# ---------------------------------------------------------------------------
# chunking — the property the fixed-region engine did not have
# ---------------------------------------------------------------------------


class TestChunking(unittest.TestCase):
    def test_boundaries_cover_the_input_exactly(self) -> None:
        data = deterministic_bytes(200_000, seed=3)
        spans = list(chunk_boundaries(data))
        self.assertEqual(spans[0][0], 0)
        self.assertEqual(spans[-1][1], len(data))
        for (_, end), (start, _) in zip(spans, spans[1:]):
            self.assertEqual(end, start)

    def test_empty_input_yields_no_chunks(self) -> None:
        self.assertEqual(list(chunk_boundaries(b"")), [])

    def test_chunking_is_deterministic(self) -> None:
        data = deterministic_bytes(100_000, seed=4)
        self.assertEqual(chunk_unit(data), chunk_unit(data))

    def test_insertion_only_disturbs_local_boundaries(self) -> None:
        # The reason content-defined chunking is here at all. Inserting a byte
        # near the front must not invalidate the chunks after it -- the failure
        # the fixed-region engine had, recorded in experiments/ccp/FINDINGS.md.
        data = deterministic_bytes(300_000, seed=5)
        shifted = data[:1000] + b"!" + data[1000:]
        original = {c.digest for c in chunk_unit(data)}
        after = {c.digest for c in chunk_unit(shifted)}
        shared = original & after
        self.assertGreater(
            len(shared) / len(original),
            0.9,
            "a one-byte insertion destroyed more than 10% of the chunks",
        )

    def test_max_chunk_is_enforced_on_degenerate_input(self) -> None:
        # Zeros produce no content boundary, so the cap has to do the cutting.
        for _, end in zip(range(5), (c.length for c in chunk_unit(b"\x00" * 100_000))):
            self.assertLessEqual(end, 8192)


# ---------------------------------------------------------------------------
# differ — Change detection
# ---------------------------------------------------------------------------


class TestDiffer(unittest.TestCase):
    def test_identical_inputs_are_pure_copy(self) -> None:
        data = deterministic_bytes(80_000, seed=6)
        program = build_change_program(data, data)
        self.assertEqual(paste(data, program), data)
        self.assertEqual(program.added_bytes, 0)
        self.assertLess(program.encoded_size(), 200)

    def test_reconstruction_is_exact_across_edit_shapes(self) -> None:
        base = deterministic_bytes(120_000, seed=7)
        cases = {
            "prepend": b"header!" + base,
            "append": base + b"trailer!",
            "insert_middle": base[:60_000] + deterministic_bytes(500, 8) + base[60_000:],
            "delete_middle": base[:40_000] + base[45_000:],
            "replace_span": base[:30_000] + deterministic_bytes(5_000, 9) + base[35_000:],
            "unrelated": deterministic_bytes(120_000, seed=10),
            "empty": b"",
        }
        for name, target in cases.items():
            with self.subTest(case=name):
                program = build_change_program(base, target)
                self.assertEqual(paste(base, program), target, f"{name} did not round-trip")

    def test_shifted_content_is_still_matched(self) -> None:
        # The headline capability: one inserted byte at the front must not stop
        # the rest from being recognised as shared.
        base = deterministic_bytes(200_000, seed=11)
        target = b"X" + base
        program = build_change_program(base, target)
        self.assertEqual(paste(base, program), target)
        ratio = program.reuse_ratio
        self.assertIsNotNone(ratio)
        assert ratio is not None
        self.assertGreater(ratio, 0.95, "shifted duplicate was not recognised")

    def test_unrelated_target_is_mostly_literal(self) -> None:
        # The negative control. Nothing shared means nothing copied; a differ
        # that claimed reuse here would be reporting a bug.
        base = deterministic_bytes(50_000, seed=12)
        target = deterministic_bytes(50_000, seed=13)
        program = build_change_program(base, target)
        self.assertEqual(paste(base, program), target)
        self.assertEqual(program.copied_bytes, 0)


# ---------------------------------------------------------------------------
# the build — Copy, Change, Paste end to end
# ---------------------------------------------------------------------------


def _near_duplicate_source(count: int, size: int, seed: int) -> InMemoryUnitSource:
    """`count` units that share a body and differ in a small region each."""
    body = deterministic_bytes(size, seed)
    source = InMemoryUnitSource()
    source.add("unit-00", body)
    for index in range(1, count):
        patch = deterministic_bytes(256, seed + index)
        cut = (index * 977) % (size - 256)
        source.add("unit-%02d" % index, body[:cut] + patch + body[cut + 256 :])
    return source


class TestBuild(unittest.TestCase):
    def test_round_trip_and_saving_on_near_duplicates(self) -> None:
        source = _near_duplicate_source(8, 120_000, seed=21)
        expected = {unit.uid: unit.data for unit in source.units()}
        model = build_model(source)

        for uid, data in expected.items():
            self.assertEqual(model.materialize(uid), data, f"{uid} did not round-trip")

        self.assertEqual(model.stats.units, 8)
        self.assertEqual(model.stats.literals, 1, "only the first unit should be stored in full")
        self.assertEqual(model.stats.derived, 7)
        saving = model.stats.payload_saving
        self.assertIsNotNone(saving)
        assert saving is not None
        self.assertGreater(saving, 0.8)

    def test_unrelated_units_are_all_stored_in_full(self) -> None:
        # The negative control for the build: no shared structure means every
        # unit is a literal and the payload is not smaller than the input.
        source = InMemoryUnitSource()
        for index in range(5):
            source.add(f"u{index}", deterministic_bytes(20_000, seed=100 + index))
        model = build_model(source)
        self.assertEqual(model.stats.derived, 0)
        self.assertEqual(model.stats.literals, 5)
        self.assertEqual(model.stats.stored_payload_bytes, model.stats.original_bytes)

    def test_a_unit_is_never_forced_into_a_delta(self) -> None:
        source = _near_duplicate_source(6, 60_000, seed=22)
        model = build_model(source)
        for uid in model.uids():
            record = model.record(uid)
            if record.is_derived:
                assert record.program is not None
                self.assertLess(
                    record.program.encoded_size(),
                    record.size,
                    f"{uid} was stored as a delta that is not smaller",
                )

    def test_empty_and_tiny_units(self) -> None:
        source = InMemoryUnitSource()
        source.add("empty", b"")
        source.add("one", b"x")
        source.add("small", b"hello world")
        model = build_model(source)
        self.assertEqual(model.materialize("empty"), b"")
        self.assertEqual(model.materialize("one"), b"x")
        self.assertEqual(model.materialize("small"), b"hello world")

    def test_no_delta_chains(self) -> None:
        source = _near_duplicate_source(10, 80_000, seed=23)
        model = build_model(source)
        for uid in model.uids():
            record = model.record(uid)
            if record.base_uid is not None:
                self.assertEqual(
                    model.record(record.base_uid).kind,
                    KIND_LITERAL,
                    "a base must itself be stored in full",
                )

    def test_build_is_deterministic(self) -> None:
        first = build_model(_near_duplicate_source(6, 50_000, seed=24))
        second = build_model(_near_duplicate_source(6, 50_000, seed=24))
        self.assertEqual(serialize(first), serialize(second))

    def test_duplicate_unit_id_is_rejected(self) -> None:
        class Twice:
            def units(self):
                from ccp.core.units import Unit

                yield Unit("same", b"abc")
                yield Unit("same", b"def")

        with self.assertRaises(ValueError):
            build_model(Twice())

    def test_identical_units_collapse(self) -> None:
        data = deterministic_bytes(40_000, seed=25)
        source = InMemoryUnitSource({"a": data, "b": data, "c": data})
        model = build_model(source)
        self.assertEqual(model.stats.literals, 1)
        for uid in ("a", "b", "c"):
            self.assertEqual(model.materialize(uid), data)


# ---------------------------------------------------------------------------
# container — the representation as real bytes
# ---------------------------------------------------------------------------


class TestContainer(unittest.TestCase):
    def test_round_trip_through_bytes(self) -> None:
        source = _near_duplicate_source(6, 90_000, seed=31)
        expected = {unit.uid: unit.data for unit in source.units()}
        container = serialize(build_model(source))
        restored = deserialize(container)
        self.assertEqual(sorted(restored.uids()), sorted(expected))
        for uid, data in expected.items():
            self.assertEqual(restored.materialize(uid), data)

    def test_container_is_smaller_than_the_input_on_near_duplicates(self) -> None:
        source = _near_duplicate_source(8, 120_000, seed=32)
        original = sum(unit.size for unit in source.units())
        container = serialize(build_model(source))
        # The measured container length, index included -- not a payload figure.
        self.assertLess(len(container), original * 0.4)

    def test_overhead_accounting_adds_up(self) -> None:
        model = build_model(_near_duplicate_source(5, 60_000, seed=33))
        container = serialize(model)
        parts = container_overhead(model, container)
        self.assertEqual(
            parts["payload_bytes"] + parts["index_bytes"], parts["container_bytes"]
        )
        self.assertGreaterEqual(parts["index_bytes"], 0)

    def test_bad_magic_is_rejected(self) -> None:
        with self.assertRaises(CCPFormatError):
            deserialize(b"NOPE" + b"\x01\x00")

    def test_truncated_container_is_rejected(self) -> None:
        container = serialize(build_model(_near_duplicate_source(4, 40_000, seed=34)))
        with self.assertRaises(CCPFormatError):
            deserialize(container[: len(container) // 2])

    def test_trailing_bytes_are_rejected(self) -> None:
        container = serialize(build_model(_near_duplicate_source(3, 30_000, seed=35)))
        with self.assertRaises(CCPFormatError):
            deserialize(container + b"\x00")

    def test_corrupted_payload_is_caught_on_materialize(self) -> None:
        # Verification is not decoration: a flipped byte must surface as an
        # integrity error rather than as a wrong unit handed to a caller.
        source = _near_duplicate_source(4, 40_000, seed=36)
        model = deserialize(serialize(build_model(source)))
        literal_uid = next(u for u in model.uids() if model.record(u).kind == KIND_LITERAL)
        corrupted = bytearray(model.literals[literal_uid])
        corrupted[len(corrupted) // 2] ^= 0xFF
        model.literals[literal_uid] = bytes(corrupted)
        with self.assertRaises(CCPIntegrityError):
            model.materialize(literal_uid)


# ---------------------------------------------------------------------------
# execution capability
# ---------------------------------------------------------------------------


class TestReader(unittest.TestCase):
    def setUp(self) -> None:
        self.source = _near_duplicate_source(6, 120_000, seed=41)
        self.expected = {unit.uid: unit.data for unit in self.source.units()}
        self.model = build_model(self.source)
        self.reader = CCPReader(self.model)

    def test_range_read_matches_the_full_unit_everywhere(self) -> None:
        for uid, data in self.expected.items():
            for offset in (0, 1, 5_000, 60_000, len(data) - 10, len(data)):
                for length in (0, 1, 64, 4096):
                    with self.subTest(uid=uid, offset=offset, length=length):
                        result = self.reader.read_range(uid, offset, length)
                        self.assertEqual(result.data, data[offset : offset + length])

    def test_small_read_touches_far_less_than_the_unit(self) -> None:
        derived = [u for u in self.model.uids() if self.model.record(u).is_derived]
        self.assertTrue(derived, "the fixture should produce derived units")
        uid = derived[0]
        result = self.reader.read_range(uid, 1000, 256)
        self.assertEqual(len(result.data), 256)
        # The measurement that matters: the work is proportional to the window,
        # not to the unit. Counted on the real path, not modelled.
        self.assertLess(result.bytes_touched, result.unit_size // 10)
        self.assertLess(result.instructions_visited, result.instructions_total)

    def test_full_range_read_touches_the_whole_unit(self) -> None:
        uid = self.model.uids()[0]
        size = self.model.record(uid).size
        result = self.reader.read_range(uid, 0, size)
        self.assertEqual(result.bytes_touched, size)
        self.assertEqual(result.work_ratio, 1.0)

    def test_read_past_the_end_returns_what_exists(self) -> None:
        uid = self.model.uids()[0]
        size = self.model.record(uid).size
        self.assertEqual(self.reader.read_range(uid, size + 10, 100).data, b"")
        self.assertEqual(len(self.reader.read_range(uid, size - 5, 100).data), 5)

    def test_negative_arguments_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.reader.read_range(self.model.uids()[0], -1, 10)

    def test_equality_without_materialising(self) -> None:
        model = build_model(InMemoryUnitSource({"a": b"same", "b": b"same", "c": b"diff"}))
        reader = CCPReader(model)
        self.assertIs(reader.units_equal("a", "b"), True)
        self.assertIs(reader.units_equal("a", "c"), False)

    def test_shared_base_groups(self) -> None:
        groups = self.reader.shared_base_groups()
        self.assertTrue(groups)
        for base_uid, members in groups.items():
            self.assertEqual(self.model.record(base_uid).kind, KIND_LITERAL)
            for uid in members:
                self.assertEqual(self.model.record(uid).kind, KIND_DERIVED)

    def test_reuse_report_sums_to_the_unit_size(self) -> None:
        for uid in self.model.uids():
            report = self.reader.reuse_report(uid)
            self.assertEqual(
                report["copied_bytes"] + report["added_bytes"], report["size"]
            )


if __name__ == "__main__":
    unittest.main()
