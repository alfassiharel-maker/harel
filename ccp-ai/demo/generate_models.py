"""Generate a real model family to measure the engine against.

These are real `.safetensors` checkpoints: real float32 tensors, a real
transformer-shaped parameter layout, and variants produced by real arithmetic on
the base weights. Nothing about the compression measurement is simulated — the
only thing standing in for production is the *scale*, because a 7 B checkpoint is
14 GB and a demo has to seed in under a minute. The dashboard extrapolates to 7 B
from the ratio measured here, and labels that extrapolation as one.

The perturbation model matters more than the size, so it is stated explicitly:

    w' = w * (1 + s * g),  g ~ N(0, 1)

`s` is the mean relative step a weight takes over a fine-tune. This is the knob
that decides the compression ratio, and it is why the demo ships several
variants at different `s` rather than one flattering number. A light instruction
tune moves weights by ~1e-4 relative; a hard domain retrain moves them by ~1e-2.
In IEEE-754 a 1e-4 relative move leaves the sign, the exponent and the top
mantissa bits untouched — which is exactly the redundancy the codec harvests. A
1e-2 move reaches further into the mantissa and compresses less. Both are shown.

Determinism: every tensor is drawn from a seeded `random.Random`, so two runs
produce byte-identical files and a ratio quoted in a meeting is reproducible.
"""

from __future__ import annotations

import argparse
import math
import os
import random
import sys
from array import array
from dataclasses import dataclass, field
from typing import Callable, Iterable

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import safetensors  # noqa: E402

Progress = Callable[[str, float], None]


@dataclass(frozen=True)
class Arch:
    """Parameter layout of the demo model. Transformer-shaped, deliberately small."""

    d_model: int = 128
    n_layers: int = 4
    ffn: int = 512
    vocab: int = 8192

    def param_count(self) -> int:
        per_layer = 4 * self.d_model * self.d_model + 2 * self.d_model * self.ffn + 2 * self.d_model
        return 2 * self.vocab * self.d_model + self.n_layers * per_layer + self.d_model

    def tensor_specs(self, vocab: int | None = None) -> list[tuple[str, tuple[int, ...]]]:
        v = vocab or self.vocab
        specs: list[tuple[str, tuple[int, ...]]] = [("model.embed_tokens.weight", (v, self.d_model))]
        for i in range(self.n_layers):
            p = f"model.layers.{i}"
            specs += [
                (f"{p}.self_attn.q_proj.weight", (self.d_model, self.d_model)),
                (f"{p}.self_attn.k_proj.weight", (self.d_model, self.d_model)),
                (f"{p}.self_attn.v_proj.weight", (self.d_model, self.d_model)),
                (f"{p}.self_attn.o_proj.weight", (self.d_model, self.d_model)),
                (f"{p}.mlp.up_proj.weight", (self.ffn, self.d_model)),
                (f"{p}.mlp.down_proj.weight", (self.d_model, self.ffn)),
                (f"{p}.input_layernorm.weight", (self.d_model,)),
                (f"{p}.post_attention_layernorm.weight", (self.d_model,)),
            ]
        specs += [("model.norm.weight", (self.d_model,)), ("lm_head.weight", (v, self.d_model))]
        return specs


@dataclass
class VariantSpec:
    """One derived model: which tensors move, how far, and what changes shape."""

    variant_id: str
    label: str
    kind: str
    relative_step: float
    # Substring match against tensor names. Empty tuple means "every tensor".
    tune_only: tuple[str, ...] = ()
    frozen: tuple[str, ...] = ()
    add_tensors: list[tuple[str, tuple[int, ...]]] = field(default_factory=list)
    drop_tensors: tuple[str, ...] = ()
    resize_vocab: int | None = None
    note: str = ""

    def tunes(self, name: str) -> bool:
        if any(f in name for f in self.frozen):
            return False
        if not self.tune_only:
            return True
        return any(t in name for t in self.tune_only)


# The family the dashboard seeds. Each entry exercises a different path through
# the packer, because "it compresses a light fine-tune" is a much weaker claim
# than "it handles everything a real model hub actually holds".
DEFAULT_VARIANTS: list[VariantSpec] = [
    VariantSpec(
        variant_id="instruct-fft-light",
        label="Instruct · full fine-tune (light, s=1e-4)",
        kind="full-fine-tune",
        relative_step=1e-4,
        note="Every weight in the model moved. This is the case LoRA exists to avoid shipping.",
    ),
    VariantSpec(
        variant_id="instruct-fft",
        label="Instruct · full fine-tune (s=1e-3)",
        kind="full-fine-tune",
        relative_step=1e-3,
        note="A longer tune. Deeper mantissa churn, so a lower ratio — shown, not hidden.",
    ),
    VariantSpec(
        variant_id="code-fft-heavy",
        label="Code · domain retrain (heavy, s=1e-2)",
        kind="full-fine-tune",
        relative_step=1e-2,
        note="Near the worst case for delta coding: a large step on every parameter.",
    ),
    VariantSpec(
        variant_id="support-classifier",
        label="Support classifier · frozen backbone + new head",
        kind="task-head",
        relative_step=1e-3,
        tune_only=("model.norm", "layers.3"),
        add_tensors=[("score.weight", (8, 128))],
        drop_tensors=("lm_head.weight",),
        note="Architecture change: the LM head is gone and a classification head is new.",
    ),
    VariantSpec(
        variant_id="last-two-layers",
        label="Partial fine-tune · last two layers only",
        kind="partial-fine-tune",
        relative_step=1e-3,
        tune_only=("layers.2", "layers.3", "model.norm"),
        note="Frozen tensors cost zero bytes: the container points at the base copy.",
    ),
    VariantSpec(
        variant_id="vocab-extended",
        label="Vocabulary extended · 8192 → 8448 tokens",
        kind="architecture-change",
        relative_step=1e-4,
        resize_vocab=8448,
        note="Reshaped tensors have no counterpart to subtract; they are stored whole.",
    ),
]


def _draw_tensor(rng: random.Random, count: int, scale: float) -> array:
    """Initialise one tensor. Gaussian, He-style scale — the distribution real
    weights actually have, which is what the exponent plane's redundancy depends
    on."""
    values = array("f", bytes(4 * count))
    gauss = rng.gauss
    for i in range(count):
        values[i] = gauss(0.0, scale)
    return values


def _perturb(rng: random.Random, values: array, relative_step: float) -> array:
    """Apply w' = w * (1 + s*g) in float32."""
    out = array("f", values)
    gauss = rng.gauss
    for i in range(len(out)):
        out[i] = out[i] * (1.0 + relative_step * gauss(0.0, 1.0))
    return out


def _to_bytes(values: array) -> bytes:
    """Little-endian float32 bytes, whatever the host's byte order is."""
    if sys.byteorder == "big":
        copy = array("f", values)
        copy.byteswap()
        return copy.tobytes()
    return values.tobytes()


def build_base(arch: Arch = Arch(), seed: int = 20260804, progress: Progress | None = None) -> tuple[bytes, dict[str, array]]:
    """Generate the base checkpoint. Returns (file bytes, tensors by name)."""
    rng = random.Random(seed)
    specs = arch.tensor_specs()
    tensors: dict[str, array] = {}
    payload: list[tuple[str, str, tuple[int, ...], bytes]] = []

    for index, (name, shape) in enumerate(specs):
        count = math.prod(shape)
        if name.endswith("layernorm.weight") or name == "model.norm.weight":
            # Norm weights initialise at 1.0 and barely move. Kept faithful
            # because an all-ones tensor is a real and very compressible thing
            # that exists in every checkpoint.
            values = array("f", [1.0] * count)
        else:
            fan_in = shape[-1]
            values = _draw_tensor(rng, count, scale=1.0 / math.sqrt(fan_in))
        tensors[name] = values
        payload.append((name, "F32", shape, _to_bytes(values)))
        if progress:
            progress(f"base · {name}", (index + 1) / len(specs))

    blob = safetensors.build(
        payload,
        metadata={
            "format": "pt",
            "ccp_demo": "base",
            "architecture": f"d{arch.d_model}-l{arch.n_layers}-v{arch.vocab}",
            "params": str(arch.param_count()),
        },
    )
    return blob, tensors


def build_variant(
    base_tensors: dict[str, array],
    spec: VariantSpec,
    arch: Arch = Arch(),
    seed: int = 20260804,
    progress: Progress | None = None,
) -> bytes:
    """Derive one variant from the base tensors, in float32, deterministically."""
    # Seed from the variant id so each variant is reproducible on its own and
    # independent of the order the family is generated in.
    rng = random.Random(f"{seed}:{spec.variant_id}")
    payload: list[tuple[str, str, tuple[int, ...], bytes]] = []
    names = [n for n in base_tensors if n not in spec.drop_tensors]
    total = len(names) + len(spec.add_tensors)

    for index, name in enumerate(names):
        values = base_tensors[name]
        shape = _shape_of(name, arch, base_tensors)

        if spec.resize_vocab and name in ("model.embed_tokens.weight", "lm_head.weight"):
            extra_rows = spec.resize_vocab - arch.vocab
            grown = array("f", values)
            grown.extend(_draw_tensor(rng, extra_rows * arch.d_model, scale=1.0 / math.sqrt(arch.d_model)))
            values = _perturb(rng, grown, spec.relative_step) if spec.tunes(name) else grown
            shape = (spec.resize_vocab, arch.d_model)
        elif spec.tunes(name):
            values = _perturb(rng, values, spec.relative_step)

        payload.append((name, "F32", shape, _to_bytes(values)))
        if progress:
            progress(f"{spec.variant_id} · {name}", (index + 1) / total)

    for offset, (name, shape) in enumerate(spec.add_tensors):
        count = math.prod(shape)
        values = _draw_tensor(rng, count, scale=1.0 / math.sqrt(shape[-1]))
        payload.append((name, "F32", shape, _to_bytes(values)))
        if progress:
            progress(f"{spec.variant_id} · {name}", (len(names) + offset + 1) / total)

    return safetensors.build(
        payload,
        metadata={
            "format": "pt",
            "ccp_demo": spec.variant_id,
            "derived_from": "base",
            "relative_step": f"{spec.relative_step:g}",
        },
    )


def _shape_of(name: str, arch: Arch, base_tensors: dict[str, array]) -> tuple[int, ...]:
    count = len(base_tensors[name])
    if name in ("model.embed_tokens.weight", "lm_head.weight"):
        return (arch.vocab, arch.d_model)
    if name.endswith("layernorm.weight") or name == "model.norm.weight":
        return (count,)
    if "up_proj" in name:
        return (arch.ffn, arch.d_model)
    if "down_proj" in name:
        return (arch.d_model, arch.ffn)
    return (arch.d_model, arch.d_model)


def generate_family(
    arch: Arch = Arch(),
    variants: Iterable[VariantSpec] = tuple(DEFAULT_VARIANTS),
    seed: int = 20260804,
    progress: Progress | None = None,
) -> tuple[bytes, list[tuple[VariantSpec, bytes]]]:
    """Generate the base plus every variant, in memory."""
    base_blob, base_tensors = build_base(arch, seed, progress)
    built: list[tuple[VariantSpec, bytes]] = []
    for spec in variants:
        built.append((spec, build_variant(base_tensors, spec, arch, seed, progress)))
    return base_blob, built


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the demo model family to disk as .safetensors files")
    parser.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "models"))
    parser.add_argument("--d-model", type=int, default=Arch().d_model)
    parser.add_argument("--layers", type=int, default=Arch().n_layers)
    parser.add_argument("--vocab", type=int, default=Arch().vocab)
    parser.add_argument("--seed", type=int, default=20260804)
    args = parser.parse_args(argv)

    arch = Arch(d_model=args.d_model, n_layers=args.layers, vocab=args.vocab)
    os.makedirs(args.out, exist_ok=True)

    def report(message: str, fraction: float) -> None:
        sys.stderr.write(f"\r{fraction:6.1%}  {message[:70]:<70}")
        sys.stderr.flush()

    base_blob, built = generate_family(arch, seed=args.seed, progress=report)
    sys.stderr.write("\n")

    with open(os.path.join(args.out, "base.safetensors"), "wb") as fh:
        fh.write(base_blob)
    print(f"base.safetensors  {len(base_blob):,} bytes  ({arch.param_count():,} params)")
    for spec, blob in built:
        with open(os.path.join(args.out, f"{spec.variant_id}.safetensors"), "wb") as fh:
            fh.write(blob)
        print(f"{spec.variant_id}.safetensors  {len(blob):,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
