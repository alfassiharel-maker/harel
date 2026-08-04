"""Container tests — the lossless guarantee, and every way it can be attacked.

The tests that matter here are the negative ones. "It rebuilt the file" is easy;
"it refuses to rebuild the wrong file quietly" is the product.
"""

from __future__ import annotations

import json
import os
import struct
import sys
import unittest
from array import array

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import pack, safetensors
from engine.codec import sha256


def f32(values) -> bytes:
    a = array("f", values)
    if sys.byteorder == "big":
        a.byteswap()
    return a.tobytes()


def make_base() -> bytes:
    return safetensors.build(
        [
            ("embed.weight", "F32", (64, 16), f32([i * 0.01 for i in range(1024)])),
            ("layer.0.weight", "F32", (16, 16), f32([i * 0.002 for i in range(256)])),
            ("layer.0.norm", "F32", (16,), f32([1.0] * 16)),
            ("head.weight", "F32", (8, 16), f32([i * 0.03 for i in range(128)])),
        ],
        metadata={"format": "pt"},
    )


def tuned(blob: bytes, names: set[str], step: float) -> bytes:
    """Rebuild a file with the named tensors scaled by (1+step)."""
    parsed = safetensors.parse(blob)
    payload = []
    for entry in parsed.entries:
        raw = parsed.tensor_bytes(entry)
        if entry.name in names:
            values = array("f")
            values.frombytes(raw)
            if sys.byteorder == "big":
                values.byteswap()
            values = array("f", [v * (1 + step) for v in values])
            raw = f32(values)
        payload.append((entry.name, entry.dtype, entry.shape, raw))
    return safetensors.build(payload, metadata={"format": "pt"})


class RoundTripTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = make_base()

    def assert_lossless(self, variant: bytes) -> pack.PackStats:
        container, stats = pack.pack(self.base, variant, base_id="base", variant_id="v")
        rebuilt = pack.unpack(container, self.base)
        self.assertEqual(sha256(rebuilt), sha256(variant))
        self.assertEqual(rebuilt, variant)
        return stats

    def test_identical_variant_costs_almost_nothing(self) -> None:
        stats = self.assert_lossless(self.base)
        self.assertEqual(stats.tensors_identical, 4)
        self.assertEqual(stats.bytes_delta_stored, 0)
        # Only the plan and the header block. A full copy would be ~4 KiB.
        self.assertLess(stats.container_bytes, len(self.base) // 2)

    def test_full_fine_tune_roundtrips(self) -> None:
        variant = tuned(self.base, {"embed.weight", "layer.0.weight", "head.weight"}, 1e-4)
        stats = self.assert_lossless(variant)
        self.assertEqual(stats.tensors_delta, 3)
        self.assertEqual(stats.tensors_identical, 1)  # the norm did not move

    def test_frozen_tensors_cost_zero_bytes(self) -> None:
        variant = tuned(self.base, {"head.weight"}, 1e-3)
        container, stats = pack.pack(self.base, variant, base_id="base", variant_id="v")
        described = pack.describe(container)
        copied = [t for t in described["tensors"] if t["op"] == "copy_base"]
        self.assertEqual(len(copied), 3)
        self.assertTrue(all(t["stored_bytes"] == 0 for t in copied))
        self.assertEqual(pack.unpack(container, self.base), variant)

    def test_new_tensor_and_dropped_tensor(self) -> None:
        parsed = safetensors.parse(self.base)
        payload = [
            (e.name, e.dtype, e.shape, parsed.tensor_bytes(e)) for e in parsed.entries if e.name != "head.weight"
        ]
        payload.append(("score.weight", "F32", (4, 16), f32([0.5] * 64)))
        variant = safetensors.build(payload, metadata={"format": "pt"})
        stats = self.assert_lossless(variant)
        self.assertEqual(stats.tensors_literal, 1)

    def test_resized_first_axis_uses_a_prefix_delta(self) -> None:
        # Vocabulary extension: the shared rows must delta, not be recopied.
        parsed = safetensors.parse(self.base)
        embed = parsed.by_name()["embed.weight"]
        grown = parsed.tensor_bytes(embed) + f32([0.9] * (8 * 16))
        payload = []
        for entry in parsed.entries:
            if entry.name == "embed.weight":
                payload.append((entry.name, "F32", (72, 16), grown))
            else:
                payload.append((entry.name, entry.dtype, entry.shape, parsed.tensor_bytes(entry)))
        variant = safetensors.build(payload, metadata={"format": "pt"})

        container, _ = pack.pack(self.base, variant, base_id="base", variant_id="v")
        self.assertEqual(pack.unpack(container, self.base), variant)

        ops = [b for b in pack.read_plan(container).blocks if b.get("name") == "embed.weight"]
        self.assertEqual({b["op"] for b in ops}, {"delta", "literal"})
        prefix = next(b for b in ops if b["op"] == "delta")
        self.assertEqual(prefix["raw_bytes"], embed.nbytes)
        # The shared rows are byte-identical, so the prefix delta is nearly free.
        self.assertLess(prefix["stored_bytes"], embed.nbytes // 4)

    def test_shrunk_first_axis_roundtrips(self) -> None:
        parsed = safetensors.parse(self.base)
        embed = parsed.by_name()["embed.weight"]
        payload = []
        for entry in parsed.entries:
            if entry.name == "embed.weight":
                payload.append((entry.name, "F32", (32, 16), parsed.tensor_bytes(embed)[: 32 * 16 * 4]))
            else:
                payload.append((entry.name, entry.dtype, entry.shape, parsed.tensor_bytes(entry)))
        self.assert_lossless(safetensors.build(payload, metadata={"format": "pt"}))

    def test_dtype_change_is_stored_whole(self) -> None:
        # F32 -> F16 on one tensor. No delta is possible; the rebuild must still
        # be exact.
        parsed = safetensors.parse(self.base)
        payload = []
        for entry in parsed.entries:
            if entry.name == "layer.0.norm":
                payload.append((entry.name, "F16", (16,), struct.pack("<16H", *([0x3C00] * 16))))
            else:
                payload.append((entry.name, entry.dtype, entry.shape, parsed.tensor_bytes(entry)))
        stats = self.assert_lossless(safetensors.build(payload, metadata={"format": "pt"}))
        self.assertGreaterEqual(stats.tensors_literal, 1)

    def test_metadata_only_change_roundtrips(self) -> None:
        parsed = safetensors.parse(self.base)
        payload = [(e.name, e.dtype, e.shape, parsed.tensor_bytes(e)) for e in parsed.entries]
        variant = safetensors.build(payload, metadata={"format": "pt", "note": "v2"})
        self.assert_lossless(variant)

    def test_empty_tensor_roundtrips(self) -> None:
        parsed = safetensors.parse(self.base)
        payload = [(e.name, e.dtype, e.shape, parsed.tensor_bytes(e)) for e in parsed.entries]
        payload.append(("empty.weight", "F32", (0, 16), b""))
        self.assert_lossless(safetensors.build(payload, metadata={"format": "pt"}))

    def test_container_never_exceeds_a_full_copy_by_much(self) -> None:
        # Worst case: incompressible tensors with no relationship to the base.
        noise = os.urandom(1024 * 4)
        variant = safetensors.build([("embed.weight", "F32", (64, 16), noise)], metadata={"format": "pt"})
        container, stats = pack.pack(self.base, variant, base_id="base", variant_id="v")
        self.assertEqual(pack.unpack(container, self.base), variant)
        # Plan overhead is JSON, so allow headroom, but it must not blow up.
        self.assertLess(stats.container_bytes, len(variant) * 1.5 + 2048)


class RejectionTests(unittest.TestCase):
    """A container must fail loudly rather than produce plausible bytes."""

    def setUp(self) -> None:
        self.base = make_base()
        self.variant = tuned(self.base, {"embed.weight"}, 1e-4)
        self.container, _ = pack.pack(self.base, self.variant, base_id="base", variant_id="v")

    def test_wrong_base_is_refused(self) -> None:
        other = tuned(self.base, {"layer.0.weight"}, 5e-3)
        with self.assertRaises(pack.ContainerError):
            pack.unpack(self.container, other)

    def test_tampered_payload_is_detected(self) -> None:
        blob = bytearray(self.container)
        blob[-1] ^= 0xFF
        with self.assertRaises((pack.ContainerError, pack.CodecError)):
            pack.unpack(bytes(blob), self.base)

    def test_bad_magic_is_refused(self) -> None:
        with self.assertRaises(pack.ContainerError):
            pack.unpack(b"NOPE" + self.container[4:], self.base)

    def test_truncated_container_is_refused(self) -> None:
        with self.assertRaises(pack.ContainerError):
            pack.unpack(self.container[:12], self.base)

    def test_absurd_plan_length_is_refused(self) -> None:
        blob = bytearray(self.container)
        blob[4:8] = struct.pack("<I", 2**31)
        with self.assertRaises(pack.ContainerError):
            pack.read_plan(bytes(blob))

    def test_unknown_container_version_is_refused(self) -> None:
        plan = pack.read_plan(self.container)
        header = dict(plan.header)
        header["container_version"] = 999
        rebuilt = _rebuild(header, plan.payload)
        with self.assertRaises(pack.ContainerError):
            pack.read_plan(rebuilt)

    def test_destination_outside_the_data_region_is_refused(self) -> None:
        plan = pack.read_plan(self.container)
        header = json.loads(json.dumps(plan.header))
        for block in header["blocks"]:
            if block.get("kind") == "tensor":
                block["dst"] = [0, 10**9]
                break
        with self.assertRaises(pack.ContainerError):
            pack.unpack(_rebuild(header, plan.payload), self.base)

    def test_base_span_outside_the_base_is_refused(self) -> None:
        plan = pack.read_plan(self.container)
        header = json.loads(json.dumps(plan.header))
        for block in header["blocks"]:
            if block.get("op") == "copy_base":
                block["base_span"] = [0, 10**9]
                break
        with self.assertRaises(pack.ContainerError):
            pack.unpack(_rebuild(header, plan.payload), self.base)

    def test_unknown_op_is_refused(self) -> None:
        plan = pack.read_plan(self.container)
        header = json.loads(json.dumps(plan.header))
        for block in header["blocks"]:
            if block.get("kind") == "tensor":
                block["op"] = "trust_me"
                break
        with self.assertRaises(pack.ContainerError):
            pack.unpack(_rebuild(header, plan.payload), self.base)

    def test_swapped_digest_is_detected(self) -> None:
        plan = pack.read_plan(self.container)
        header = json.loads(json.dumps(plan.header))
        header["variant"]["digest"] = "0" * 64
        with self.assertRaises(pack.ContainerError):
            pack.unpack(_rebuild(header, plan.payload), self.base)


class DescribeTests(unittest.TestCase):
    def test_describe_accounts_for_every_byte(self) -> None:
        base = make_base()
        variant = tuned(base, {"embed.weight", "head.weight"}, 1e-4)
        container, stats = pack.pack(base, variant, base_id="base", variant_id="v")
        described = pack.describe(container)
        tensor_raw = sum(t["raw_bytes"] for t in described["tensors"])
        parsed = safetensors.parse(variant)
        self.assertEqual(tensor_raw, sum(e.nbytes for e in parsed.entries))
        self.assertEqual(described["variant"]["bytes"], stats.variant_raw_bytes)


def _rebuild(header: dict, payload: bytes) -> bytes:
    plan_bytes = json.dumps(header, separators=(",", ":")).encode("utf-8")
    return pack.MAGIC + struct.pack("<I", len(plan_bytes)) + plan_bytes + payload


if __name__ == "__main__":
    unittest.main()
