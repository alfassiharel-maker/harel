"""Tests for the BitEngine container and representation buffer.

    cd bitengine && python3 -m unittest discover -s tests -t .

A container is read back from disk, so every field in it is untrusted input.
The malformed-container cases below are the bulk of this file for that reason: a
header is a parser, and a parser that trusts its input is the bug.

The other property under test is that random access agrees with sequential
decode. If they ever differ the buffer is serving stale or wrong blocks, which
would surface as silently corrupt output rather than as an error.
"""

from __future__ import annotations

import hashlib
import io
import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import l1  # noqa: E402
import l2  # noqa: E402
import l3  # noqa: E402


def keystream(n: int, tag: bytes = b"l3") -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < n:
        out += hashlib.sha256(tag + counter.to_bytes(8, "big")).digest()
        counter += 1
    return bytes(out[:n])


def frames(count: int, size: int, changed: int) -> bytes:
    first = keystream(size, b"f0")
    out = bytearray(first)
    previous = bytearray(first)
    for i in range(1, count):
        current = bytearray(previous)
        start = (i * 97) % max(1, size - changed)
        current[start : start + changed] = keystream(changed, b"m" + i.to_bytes(2, "big"))
        out += current
        previous = current
    return bytes(out)


def pack(data: bytes, goal: l2.Goal, reference: bytes | None = None) -> tuple[bytes, l3.Manifest]:
    buffer = io.BytesIO()
    manifest = l3.write_container(
        buffer, io.BytesIO(data), goal, io.BytesIO(reference) if reference is not None else None
    )
    return buffer.getvalue(), manifest


def reader_for(container: bytes, reference: bytes | None = None) -> l3.Reader:
    return l3.Reader(io.BytesIO(container), io.BytesIO(reference) if reference is not None else None)


class RoundTrip(unittest.TestCase):
    def test_every_strategy(self) -> None:
        cases = {
            "preceding": (frames(12, 4096, 400), l2.goal_named("video-frame-delta").with_block_bytes(4096), None),
            "anchor": (keystream(4096, b"a") * 10, l2.goal_named("anchor-dedup").with_block_bytes(4096), None),
            "paired": (
                keystream(40_000, b"v2"),
                l2.goal_named("checkpoint-pair").with_block_bytes(4096),
                keystream(40_000, b"v2"),
            ),
        }
        for name, (data, goal, reference) in cases.items():
            with self.subTest(strategy=name):
                container, manifest = pack(data, goal, reference)
                self.assertEqual(manifest.total_bytes, len(data))
                self.assertEqual(manifest.sha256, hashlib.sha256(data).digest())
                with reader_for(container, reference) as reader:
                    self.assertEqual(b"".join(reader.blocks()), data)
                    self.assertTrue(reader.verify())

    def test_short_final_block(self) -> None:
        goal = l2.goal_named("anchor-dedup").with_block_bytes(1024)
        for length in (1, 1023, 1025, 5000):
            with self.subTest(length=length):
                data = keystream(length, b"s")
                container, manifest = pack(data, goal)
                self.assertEqual(manifest.total_bytes, length)
                with reader_for(container) as reader:
                    self.assertEqual(b"".join(reader.blocks()), data)

    def test_container_is_smaller_than_the_input(self) -> None:
        data = frames(32, 4096, 200)
        container, manifest = pack(data, l2.goal_named("video-frame-delta").with_block_bytes(4096))
        self.assertEqual(len(container), manifest.container_bytes)
        self.assertLess(manifest.container_bytes, manifest.total_bytes)
        assert manifest.saving_pct is not None
        self.assertGreater(manifest.saving_pct, 80.0)

    def test_incompressible_input_barely_grows(self) -> None:
        # Header plus index plus one tag per block. The container must not blow up.
        data = keystream(64 * 1024, b"noise")
        goal = l2.goal_named("video-frame-delta").with_block_bytes(4096)
        _, manifest = pack(data, goal)
        self.assertLess(manifest.container_bytes, manifest.total_bytes * 1.01)

    def test_empty_input(self) -> None:
        container, manifest = pack(b"", l2.goal_named("anchor-dedup"))
        self.assertEqual(manifest.block_count, 0)
        self.assertIsNone(manifest.saving_pct)
        with reader_for(container) as reader:
            self.assertEqual(list(reader.blocks()), [])
            self.assertTrue(reader.verify())

    def test_non_seekable_destination_rejected(self) -> None:
        class NoSeek(io.BytesIO):
            def seekable(self) -> bool:
                return False

        with self.assertRaises(l3.ContainerError):
            l3.write_container(NoSeek(), io.BytesIO(b"x" * 4096), l2.goal_named("anchor-dedup"))


class RandomAccess(unittest.TestCase):
    def setUp(self) -> None:
        self.data = frames(24, 4096, 300)
        self.goal = l2.goal_named("video-frame-delta").with_block_bytes(4096)
        self.container, self.manifest = pack(self.data, self.goal)

    def _expected(self, index: int) -> bytes:
        return self.data[index * 4096 : (index + 1) * 4096]

    def test_matches_sequential_decode(self) -> None:
        with reader_for(self.container) as reader:
            for index in (23, 0, 17, 5, 23, 1):
                with self.subTest(index=index):
                    self.assertEqual(reader.block(index), self._expected(index))

    def test_out_of_range(self) -> None:
        with reader_for(self.container) as reader:
            for index in (-1, self.manifest.block_count):
                with self.subTest(index=index), self.assertRaises(IndexError):
                    reader.block(index)

    def test_cache_is_bounded(self) -> None:
        reader = l3.Reader(io.BytesIO(self.container), cache_blocks=4)
        for index in range(self.manifest.block_count):
            reader.block(index)
        self.assertLessEqual(len(reader._cache), 4)
        # Still correct after eviction forced a re-walk.
        self.assertEqual(reader.block(20), self._expected(20))

    def test_cache_rejects_zero(self) -> None:
        with self.assertRaises(ValueError):
            l3.Reader(io.BytesIO(self.container), cache_blocks=0)

    def test_chain_length_unbounded_without_keyframes(self) -> None:
        with reader_for(self.container) as reader:
            self.assertEqual(reader.chain_length(0), 1)
            self.assertEqual(reader.chain_length(23), 24)

    def test_keyframes_bound_the_chain(self) -> None:
        goal = self.goal.replace(keyframe_interval=8)
        container, _ = pack(self.data, goal)
        with reader_for(container) as reader:
            self.assertEqual(reader.chain_length(23), 8)
            self.assertEqual(reader.chain_length(16), 1)
            self.assertEqual(reader.block(23), self._expected(23))
            self.assertEqual(b"".join(reader.blocks()), self.data)

    def test_keyframes_cost_saving(self) -> None:
        # The trade must be visible, not free.
        _, without = pack(self.data, self.goal)
        _, with_keys = pack(self.data, self.goal.replace(keyframe_interval=4))
        assert without.saving_pct is not None and with_keys.saving_pct is not None
        self.assertGreater(without.saving_pct, with_keys.saving_pct)

    def test_anchor_chain_is_two(self) -> None:
        data = keystream(4096, b"a") * 10
        container, _ = pack(data, l2.goal_named("anchor-dedup").with_block_bytes(4096))
        with reader_for(container) as reader:
            self.assertEqual(reader.chain_length(9), 2)
            self.assertEqual(reader.block(9), data[9 * 4096 : 10 * 4096])

    def test_cached_chain_is_zero(self) -> None:
        with reader_for(self.container) as reader:
            reader.block(23)
            self.assertEqual(reader.chain_length(23), 0)

    def test_paired_container_needs_its_reference(self) -> None:
        data = keystream(40_000, b"p")
        goal = l2.goal_named("checkpoint-pair").with_block_bytes(4096)
        container, _ = pack(data, goal, data)
        with reader_for(container) as reader:  # no reference supplied
            with self.assertRaises(l3.ContainerError):
                reader.block(3)


class MalformedContainer(unittest.TestCase):
    """A container is a parser over untrusted bytes. It must never trust a field."""

    def setUp(self) -> None:
        self.data = frames(8, 4096, 200)
        self.goal = l2.goal_named("video-frame-delta").with_block_bytes(4096)
        self.container, self.manifest = pack(self.data, self.goal)

    def _patched(self, **fields: object) -> bytes:
        raw = list(struct.Struct("<4sBBIQQIBIBI32s32s").unpack(self.container[: l3._HEADER.size]))
        order = [
            "magic", "version", "flags", "block_bytes", "total_bytes", "index_offset",
            "block_count", "strategy", "stride", "codec_mask", "keyframe_interval",
            "sha256", "name",
        ]
        for key, value in fields.items():
            raw[order.index(key)] = value
        return l3._HEADER.pack(*raw) + self.container[l3._HEADER.size :]

    def test_bad_magic(self) -> None:
        with self.assertRaises(l3.ContainerError):
            reader_for(self._patched(magic=b"NOPE"))

    def test_unsupported_version(self) -> None:
        with self.assertRaises(l3.ContainerError):
            reader_for(self._patched(version=99))

    def test_too_short_for_a_header(self) -> None:
        with self.assertRaises(l3.ContainerError):
            reader_for(self.container[:10])

    def test_index_offset_outside_file(self) -> None:
        for offset in (0, len(self.container) * 4):
            with self.subTest(offset=offset), self.assertRaises(l3.ContainerError):
                reader_for(self._patched(index_offset=offset))

    def test_block_count_overruns_index(self) -> None:
        with self.assertRaises(l3.ContainerError):
            reader_for(self._patched(block_count=self.manifest.block_count + 5000))

    def test_block_count_over_the_cap(self) -> None:
        with self.assertRaises(l3.ContainerError):
            reader_for(self._patched(block_count=l2.MAX_BLOCKS + 1, index_offset=l3._HEADER.size))

    def test_unknown_strategy(self) -> None:
        with self.assertRaises(l3.ContainerError):
            reader_for(self._patched(strategy=7))

    def test_no_codecs_permitted(self) -> None:
        with self.assertRaises(l3.ContainerError):
            reader_for(self._patched(codec_mask=0))

    def test_invalid_goal_in_header(self) -> None:
        # block_bytes of 0 is not constructible as a Goal.
        with self.assertRaises(l3.ContainerError):
            reader_for(self._patched(block_bytes=0))

    def test_impossible_block_length_in_index(self) -> None:
        # A payload can be at most one block plus a codec tag.
        index_at = self.manifest.index_offset
        broken = bytearray(self.container)
        broken[index_at : index_at + 4] = struct.pack("<I", 4096 + 99)
        with self.assertRaises(l3.ContainerError):
            reader_for(bytes(broken))

    def test_zero_block_length_in_index(self) -> None:
        index_at = self.manifest.index_offset
        broken = bytearray(self.container)
        broken[index_at : index_at + 4] = struct.pack("<I", 0)
        with self.assertRaises(l3.ContainerError):
            reader_for(bytes(broken))

    def test_payload_overruns_index_region(self) -> None:
        index_at = self.manifest.index_offset
        broken = bytearray(self.container)
        for i in range(self.manifest.block_count):
            broken[index_at + i * 4 : index_at + i * 4 + 4] = struct.pack("<I", 4097)
        with self.assertRaises(l3.ContainerError):
            reader_for(bytes(broken))

    def test_truncated_index(self) -> None:
        with self.assertRaises(l3.ContainerError):
            reader_for(self.container[:-3])

    def test_corrupt_payload_fails_verification(self) -> None:
        broken = bytearray(self.container)
        # Flip a byte inside the first payload, which is raw, so the change
        # survives into the output rather than being rejected as malformed.
        broken[l3._HEADER.size + 100] ^= 0xFF
        with reader_for(bytes(broken)) as reader:
            self.assertFalse(reader.verify())

    def test_corrupt_payload_tag_is_rejected(self) -> None:
        broken = bytearray(self.container)
        broken[l3._HEADER.size] = 200  # unknown codec id
        with reader_for(bytes(broken)) as reader, self.assertRaises(l1.CorruptBlock):
            reader.block(0)

    def test_histogram_rejects_unknown_codec(self) -> None:
        broken = bytearray(self.container)
        broken[l3._HEADER.size] = 200
        with reader_for(bytes(broken)) as reader, self.assertRaises(l3.ContainerError):
            reader.codec_histogram()


class Inspection(unittest.TestCase):
    def test_histogram_matches_block_count(self) -> None:
        data = frames(16, 4096, 300)
        container, manifest = pack(data, l2.goal_named("video-frame-delta").with_block_bytes(4096))
        with reader_for(container) as reader:
            histogram = reader.codec_histogram()
            self.assertEqual(sum(histogram.values()), manifest.block_count)
            self.assertIn("raw", histogram)  # the first block has no predecessor

    def test_manifest_survives_a_round_trip_through_the_header(self) -> None:
        goal = l2.goal_named("video-frame-delta").replace(
            block_bytes=8192, keyframe_interval=4, codecs=l2.POLICY_FAST_DECODE
        )
        container, manifest = pack(frames(16, 8192, 400), goal)
        with reader_for(container) as reader:
            restored = reader.goal
            self.assertEqual(restored.block_bytes, goal.block_bytes)
            self.assertEqual(restored.base, goal.base)
            self.assertEqual(restored.stride_blocks, goal.stride_blocks)
            self.assertEqual(restored.keyframe_interval, goal.keyframe_interval)
            self.assertEqual(restored.codecs, goal.codecs)
            self.assertEqual(reader.manifest.sha256, manifest.sha256)

    def test_read_container_by_path(self) -> None:
        data = frames(8, 4096, 200)
        goal = l2.goal_named("video-frame-delta").with_block_bytes(4096)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "c.bite")
            with open(path, "wb") as handle:
                l3.write_container(handle, io.BytesIO(data), goal)
            with l3.read_container(path) as reader:
                self.assertTrue(reader.verify())
                self.assertEqual(b"".join(reader.blocks()), data)

    def test_read_container_missing_file(self) -> None:
        with self.assertRaises(FileNotFoundError):
            l3.read_container("/nonexistent/definitely-not-here.bite")


if __name__ == "__main__":
    unittest.main()
