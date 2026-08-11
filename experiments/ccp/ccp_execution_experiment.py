#!/usr/bin/env python3
"""CCP execution experiment — does the base+delta representation cut execution, not just storage?

The storage experiment in this directory proved one thing: a shared base plus
per-region deltas can *store* near-duplicate binary data in less space,
losslessly. That is a statement about representation size. It says nothing about
the cost of *computing* over that data.

This experiment tests the next, separate claim — the one the product direction
actually rests on:

    HYPOTHESIS. If a body of inputs is already in CCP form (one base x0 plus
    sparse deltas), then a computation y = f(x) over those inputs can be executed
    for less total work by computing f once on the base and updating the base
    result by the delta, instead of recomputing f from scratch for every input —
    with a result identical to full recomputation.

It is a hypothesis, not a proven capability. This file is built to be able to
refute it. Every reported saving is a measured operation count and a measured
wall-clock time on real execution, and every CCP result is checked against full
recomputation for bit-exact equality; a single mismatch is a hard failure.

WHAT IS PROVEN ALREADY   base+delta *storage* representation (see FINDINGS.md).
WHAT IS HYPOTHESISED     that same representation can drive cheaper *execution*.
WHAT THIS TESTS          y_i = y0 + A*delta_i  vs  y_i = A*x_i, on equal substrate.
SUCCESS                  bit-exact AND fewer multiply-adds AND less time, over a
                         non-trivial sparsity band, with the boundary located.
FAILURE                  not exact, or not fewer ops, or the op-saving does not
                         survive into wall-clock time, or the band is too narrow
                         to matter. Any of these is reported as the result.

The chosen f is a fixed linear (matrix) operator. That choice is deliberate and
its limit is a result, not a convenience: the exact incremental identity
    A*(x0 + d) == A*x0 + A*d
holds *only* because A is linear. The nonlinear control below shows the identity
breaking the moment an activation is introduced, which bounds the whole claim to
the linear part of a computation (in a neural network, the matmuls — most of the
FLOPs, but not the activations or attention softmax).

Two costs are measured separately, mirroring the storage finding that a smaller
representation is not automatically a cheaper one:

    OPERATION COUNT   scalar integer multiply-adds actually performed. Exact,
                      hardware-independent, noise-free. This is the pure test of
                      "does the representation reduce the abstract work?".
    WALL-CLOCK TIME   best-of-R perf_counter seconds on the same pure-Python
                      scalar substrate for both methods, so the comparison
                      isolates the algorithmic work difference rather than a
                      library constant factor. This is the reality check:
                      "does the reduced work survive into real time?".

Integer arithmetic is used throughout so correctness is bit-exact (integer
distributivity is exact; float A*x0 + A*d would differ from A*(x0+d) in the last
place and turn a clean pass/fail into an epsilon argument). Real quantized
inference is int8, so this is not an unrealistic regime.

Standard library only. Deterministic: all data comes from a blake2b hash chain
seeded by an integer, never the `random` module, so a run reproduces byte-for-byte.

    python3 ccp_execution_experiment.py
    python3 ccp_execution_experiment.py --m 256 --n 256 --reuse 64 --json out.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# deterministic data — a hash chain, never `random` (a flaky fixture would make
# the whole experiment worthless, exactly as in ccp_datasets.py)
# ---------------------------------------------------------------------------

_DIGEST = 32


def _byte_stream(count: int, seed: int) -> bytes:
    """`count` deterministic pseudo-random bytes from a blake2b counter chain."""
    out = bytearray()
    block = 0
    seed_bytes = seed.to_bytes(8, "little")
    while len(out) < count:
        out += hashlib.blake2b(
            block.to_bytes(8, "little") + seed_bytes, digest_size=_DIGEST
        ).digest()
        block += 1
    return bytes(out[:count])


def _ints(count: int, seed: int, lo: int, hi: int) -> List[int]:
    """`count` deterministic integers in [lo, hi], inclusive."""
    span = hi - lo + 1
    return [lo + (b % span) for b in _byte_stream(count, seed)]


def make_operator(m: int, n: int, seed: int) -> List[List[int]]:
    """An m*n integer operator A. int8-range entries mimic a quantized weight."""
    flat = _ints(m * n, seed, -127, 127)
    return [flat[r * n : (r + 1) * n] for r in range(m)]


def make_base_input(n: int, seed: int) -> List[int]:
    """The base input x0, shared by every derived input."""
    return _ints(n, seed, -127, 127)


def make_delta(n: int, k: int, base: Sequence[int], seed: int) -> Dict[int, int]:
    """A sparse ARITHMETIC delta: k positions, each mapped to (new - old) != 0.

    Note the delta is arithmetic (a value difference), not the XOR byte delta the
    storage experiment uses. That difference is itself a finding: storage deltas
    are magnitude-blind because storage only cares about bit patterns, but
    execution needs magnitude, so the two are different encodings of the one
    Copy-Change-Paste principle. The principle transfers; the byte encoding does
    not.
    """
    if k > n:
        raise ValueError("k cannot exceed n")
    # Deterministic distinct positions: a hash-ordered permutation prefix.
    order = sorted(range(n), key=lambda p: _byte_stream(_DIGEST, seed * 131 + p))
    positions = order[:k]
    new_values = _ints(k, seed * 977 + 1, -127, 127)
    delta: Dict[int, int] = {}
    for pos, new_val in zip(positions, new_values):
        change = new_val - base[pos]
        # A delta entry that changes nothing is not a change; nudge it so the
        # experiment actually exercises k real changes rather than k-ish.
        if change == 0:
            change = 1
        delta[pos] = change
    return delta


def apply_delta(base: Sequence[int], delta: Dict[int, int]) -> List[int]:
    """Reconstruct a full input from the base and its arithmetic delta."""
    x = list(base)
    for pos, change in delta.items():
        x[pos] += change
    return x


# ---------------------------------------------------------------------------
# execution substrate — pure-Python scalar multiply-add, counted the same way
# for both methods so wall-clock reflects work, not a library constant
# ---------------------------------------------------------------------------


def matvec_full(a: Sequence[Sequence[int]], x: Sequence[int]) -> Tuple[List[int], int]:
    """y = A*x from scratch. Returns (y, multiply_adds). Cost: m*n."""
    m = len(a)
    n = len(x)
    y = [0] * m
    for i in range(m):
        row = a[i]
        acc = 0
        for j in range(n):
            acc += row[j] * x[j]
        y[i] = acc
    return y, m * n


def matvec_incremental(
    a: Sequence[Sequence[int]], y0: Sequence[int], delta: Dict[int, int]
) -> Tuple[List[int], int]:
    """y = y0 + A*delta, touching only changed columns. Cost: k*m.

    This is Paste at the execution level: the base result y0 is Copied, and each
    Change contributes A[:, pos] * change to it. Only the linearity of A makes
    this equal to a full recompute.
    """
    m = len(a)
    y = list(y0)
    madds = 0
    for pos, change in delta.items():
        for i in range(m):
            y[i] += a[i][pos] * change
        madds += m
    return y, madds


# ---------------------------------------------------------------------------
# analytic cost model — cross-checked against the counted operations, exactly as
# the storage experiment cross-checks its container size against its cost model
# ---------------------------------------------------------------------------


def model_full_madds(m: int, n: int, reuse: int) -> int:
    """Full recomputation: one m*n matvec per input."""
    return reuse * m * n


def model_ccp_madds(m: int, n: int, reuse: int, k: int) -> int:
    """CCP: one m*n base matvec, then a k*m update per input."""
    return m * n + reuse * k * m


# ---------------------------------------------------------------------------
# a single trial: build inputs, run both ways, verify, time
# ---------------------------------------------------------------------------


@dataclass
class Trial:
    label: str
    m: int
    n: int
    reuse: int          # how many derived inputs share the one base
    k: int              # changed positions per input
    sparsity: float     # k / n
    correct: bool
    ops_full: int
    ops_ccp: int
    time_full: float
    time_ccp: float
    nonlinear: bool = False
    verdict: str = ""
    notes: str = ""

    @property
    def ops_ratio(self) -> float:
        return self.ops_ccp / self.ops_full if self.ops_full else float("nan")

    @property
    def time_ratio(self) -> float:
        return self.time_ccp / self.time_full if self.time_full else float("nan")


def _clamp(v: int, lo: int = 0) -> int:
    """A ReLU-like nonlinearity, used only by the nonlinear control."""
    return v if v > lo else lo


def run_trial(
    m: int,
    n: int,
    reuse: int,
    k: int,
    seed: int,
    reps: int,
    label: str,
    nonlinear: bool = False,
) -> Trial:
    """Execute `reuse` inputs both ways, verify bit-exactness, and time both."""
    a = make_operator(m, n, seed)
    x0 = make_base_input(n, seed + 1)
    deltas = [make_delta(n, k, x0, seed + 100 + i) for i in range(reuse)]
    full_inputs = [apply_delta(x0, d) for d in deltas]

    def post(y: List[int]) -> List[int]:
        return [_clamp(v) for v in y] if nonlinear else y

    # --- correctness: CCP result must equal full recomputation, exactly ---
    #
    # y0_pre is the base result A*x0 before any activation. In the linear case it
    # is the base result outright. In the nonlinear case a stage would cache its
    # *activated* output g0 = clamp(A*x0), so that is the base a real pipeline
    # would reuse — and the naive CCP update adds the linear delta A*d to it,
    # which cannot undo the clamp. That is the mechanism this control exercises,
    # and it is meant to break.
    y0_pre, _ = matvec_full(a, x0)
    g0 = post(y0_pre)
    full_results: List[List[int]] = []
    ccp_results: List[List[int]] = []
    ops_full = 0
    ops_ccp = m * n  # the one base matvec, paid once and reused
    for d, xi in zip(deltas, full_inputs):
        yf_pre, of = matvec_full(a, xi)
        yc_pre, oc = matvec_incremental(a, y0_pre, d)  # exact pre-activation
        ops_full += of
        ops_ccp += oc
        full_results.append(post(yf_pre))
        if nonlinear:
            # A*d recovered from the incremental pre-activation, added to the
            # cached activated base without re-applying the nonlinearity.
            lin_delta = [yc_pre[i] - y0_pre[i] for i in range(m)]
            ccp_results.append([g0[i] + lin_delta[i] for i in range(m)])
        else:
            ccp_results.append(yc_pre)
    correct = full_results == ccp_results

    # The analytic model must agree with the counted operations, or an accounting
    # bug is hiding in one of the two paths.
    assert ops_full == model_full_madds(m, n, reuse), "full op accounting drifted"
    assert ops_ccp == model_ccp_madds(m, n, reuse, k), "ccp op accounting drifted"

    # --- timing: best-of-R on the same substrate; min is the robust estimator ---
    # The two functions mirror the operation accounting exactly: full recompute
    # does one matvec per input and computes no base; CCP pays for the base
    # matvec once and then one incremental update per input. Charging the base
    # to full as well would flatter CCP, so it is not.
    def time_full_fn() -> None:
        for xi in full_inputs:
            matvec_full(a, xi)

    def time_ccp_fn() -> None:
        yb, _ = matvec_full(a, x0)
        for d in deltas:
            matvec_incremental(a, yb, d)

    time_full = _best_time(time_full_fn, reps)
    time_ccp = _best_time(time_ccp_fn, reps)

    trial = Trial(
        label=label,
        m=m,
        n=n,
        reuse=reuse,
        k=k,
        sparsity=k / n if n else float("nan"),
        correct=correct,
        ops_full=ops_full,
        ops_ccp=ops_ccp,
        time_full=time_full,
        time_ccp=time_ccp,
        nonlinear=nonlinear,
    )
    trial.verdict, trial.notes = _verdict(trial)
    return trial


def _best_time(fn, reps: int) -> float:
    best = float("inf")
    for _ in range(max(1, reps)):
        start = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - start)
    return best


# A time ratio must clear this margin before a wall-clock win is claimed, so
# perf_counter noise near parity is not read as a result.
TIME_MARGIN = 0.95


def _verdict(t: Trial) -> Tuple[str, str]:
    if not t.correct:
        # Expected for the nonlinear control; a bug everywhere else.
        if t.nonlinear:
            return (
                "IDENTITY BROKEN",
                "base+delta != full recompute once a nonlinearity is applied; "
                "the exact contract holds only for the linear operator",
            )
        return ("INCORRECT", "CCP result diverged from full recomputation — bug")
    fewer_ops = t.ops_ccp < t.ops_full
    faster = t.time_ccp < t.time_full * TIME_MARGIN
    if fewer_ops and faster:
        return ("SUPPORTED", "fewer multiply-adds and lower wall-clock")
    if fewer_ops and not faster:
        return (
            "OPS-ONLY",
            "fewer multiply-adds but the saving did not survive into wall-clock "
            "on this substrate (per-input overhead dominates)",
        )
    return ("NO SAVING", "CCP did at least as much work as full recomputation")


# ---------------------------------------------------------------------------
# the experiment: a sparsity sweep plus the falsification controls
# ---------------------------------------------------------------------------


@dataclass
class Experiment:
    m: int
    n: int
    reuse: int
    reps: int
    seed: int
    sweep: List[Trial] = field(default_factory=list)
    controls: List[Trial] = field(default_factory=list)

    @property
    def supported_band(self) -> List[Trial]:
        return [t for t in self.sweep if t.verdict == "SUPPORTED"]


def run_experiment(
    m: int, n: int, reuse: int, reps: int, seed: int
) -> Experiment:
    exp = Experiment(m=m, n=n, reuse=reuse, reps=reps, seed=seed)

    # Sparsity sweep: how much of the input changes per derived instance. The
    # storage method died near 10% byte divergence; this locates the execution
    # boundary independently.
    fractions = [0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5]
    for frac in fractions:
        k = max(1, round(frac * n))
        exp.sweep.append(
            run_trial(m, n, reuse, k, seed, reps, label=f"sparsity={frac:.3f}")
        )

    # Control 1 — dense delta. Every position changes; CCP must not win.
    exp.controls.append(
        run_trial(m, n, reuse, n, seed, reps, label="control:dense_delta")
    )
    # Control 2 — no reuse. A single input cannot amortise the base matvec.
    exp.controls.append(
        run_trial(m, n, 1, max(1, round(0.01 * n)), seed, reps, label="control:no_reuse")
    )
    # Control 3 — nonlinearity. The exact incremental identity must break.
    exp.controls.append(
        run_trial(
            m,
            n,
            reuse,
            max(1, round(0.01 * n)),
            seed,
            reps,
            label="control:nonlinear",
            nonlinear=True,
        )
    )
    return exp


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


def _fmt_ratio(x: float) -> str:
    return f"{x:6.3f}" if x == x else "   nan"


def _speedup(x: float) -> str:
    return f"{1 / x:6.2f}x" if x and x == x else "   nan"


def render_report(exp: Experiment) -> str:
    lines: List[str] = []
    w = lines.append
    w("CCP EXECUTION EXPERIMENT")
    w("=" * 72)
    w(f"operator A: {exp.m} x {exp.n} int8-range   reuse (inputs/base): {exp.reuse}")
    w(f"substrate: pure-Python scalar multiply-add   timing: best-of-{exp.reps}")
    w("hypothesis: y_i = y0 + A*delta_i executes for less than y_i = A*x_i,")
    w("            bit-exactly, when the delta is sparse.")
    w("")
    w("SPARSITY SWEEP")
    w("-" * 72)
    w(
        f"{'k/n':>7} {'k':>5} {'ops full':>12} {'ops ccp':>12} "
        f"{'ops x':>7} {'time x':>7} {'ok':>3} {'verdict':>10}"
    )
    for t in exp.sweep:
        w(
            f"{t.sparsity:7.3f} {t.k:5d} {t.ops_full:12d} {t.ops_ccp:12d} "
            f"{_speedup(t.ops_ratio):>7} {_speedup(t.time_ratio):>7} "
            f"{'Y' if t.correct else 'N':>3} {t.verdict:>10}"
        )
    w("")
    w("  ops x / time x are CCP speedups (full / ccp); >1 means CCP did less.")
    w("")
    w("FALSIFICATION CONTROLS")
    w("-" * 72)
    for t in exp.controls:
        w(f"  {t.label:22} verdict={t.verdict}")
        w(f"      {t.notes}")
        w(
            f"      ops full={t.ops_full} ccp={t.ops_ccp} "
            f"({_speedup(t.ops_ratio)})  time {_speedup(t.time_ratio)}  "
            f"correct={t.correct}"
        )
    w("")
    w("VERDICT")
    w("-" * 72)
    for line in _overall_verdict(exp):
        w(line)
    return "\n".join(lines)


def _overall_verdict(exp: Experiment) -> List[str]:
    out: List[str] = []
    band = exp.supported_band
    all_correct = all(t.correct for t in exp.sweep) and all(
        t.correct for t in exp.controls if not t.nonlinear
    )
    if not all_correct:
        out.append("REFUTED BY CORRECTNESS: a linear-case CCP result diverged from")
        out.append("full recomputation. The mechanism is not sound as written.")
        return out

    ops_win_everywhere = all(t.ops_ccp < t.ops_full for t in exp.sweep)
    if band:
        lo = min(t.sparsity for t in band)
        hi = max(t.sparsity for t in band)
        out.append(
            f"HYPOTHESIS SUPPORTED in a band: bit-exact, fewer ops, and lower "
            f"wall-clock for sparsity in [{lo:.3f}, {hi:.3f}]."
        )
        out.append(
            "  The op-count reduction is arithmetic and exact; the wall-clock "
            "win is the load-bearing part and it is present here on equal substrate."
        )
    elif ops_win_everywhere:
        out.append(
            "HYPOTHESIS PARTIALLY SUPPORTED: CCP does strictly fewer multiply-adds"
        )
        out.append(
            "  across the sweep, but the saving did not survive into wall-clock"
        )
        out.append(
            "  time on this substrate. This is the execution-level analogue of"
        )
        out.append(
            "  'representation efficiency != execution efficiency': less work, not"
        )
        out.append("  less time. A tighter substrate is needed before any claim.")
    else:
        out.append(
            "HYPOTHESIS NOT SUPPORTED at these parameters: CCP did not reliably"
        )
        out.append("  reduce execution work.")

    out.append("")
    out.append("Bounds established by the controls, all measured:")
    out.append(
        "  - dense delta: CCP loses (no exploitable sparsity) — as it must."
    )
    out.append(
        "  - single input / no reuse: CCP loses (base matvec unamortised)."
    )
    out.append(
        "  - nonlinearity: the exact base+delta identity BREAKS. The contract"
    )
    out.append(
        "    'output == full recompute' holds only for the linear operator. Any"
    )
    out.append(
        "    real pipeline's nonlinear stages need recompute or approximation,"
    )
    out.append("    which this experiment does not attempt.")
    return out


def to_json(exp: Experiment) -> dict:
    def trial_dict(t: Trial) -> dict:
        return {
            "label": t.label,
            "m": t.m,
            "n": t.n,
            "reuse": t.reuse,
            "k": t.k,
            "sparsity": t.sparsity,
            "correct": t.correct,
            "ops_full": t.ops_full,
            "ops_ccp": t.ops_ccp,
            "ops_ratio": t.ops_ratio,
            "time_full": t.time_full,
            "time_ccp": t.time_ccp,
            "time_ratio": t.time_ratio,
            "nonlinear": t.nonlinear,
            "verdict": t.verdict,
            "notes": t.notes,
        }

    return {
        "m": exp.m,
        "n": exp.n,
        "reuse": exp.reuse,
        "reps": exp.reps,
        "seed": exp.seed,
        "sweep": [trial_dict(t) for t in exp.sweep],
        "controls": [trial_dict(t) for t in exp.controls],
    }


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__ and __doc__.splitlines()[0])
    parser.add_argument("--m", type=int, default=128, help="operator rows")
    parser.add_argument("--n", type=int, default=128, help="operator cols / input length")
    parser.add_argument(
        "--reuse", type=int, default=32, help="derived inputs sharing one base"
    )
    parser.add_argument("--reps", type=int, default=5, help="timing repetitions (best-of)")
    parser.add_argument("--seed", type=int, default=20260811, help="deterministic seed")
    parser.add_argument("--json", type=str, default=None, help="write machine-readable JSON")
    parser.add_argument("--output", type=str, default=None, help="write the text report")
    args = parser.parse_args(argv)

    exp = run_experiment(args.m, args.n, args.reuse, args.reps, args.seed)
    report = render_report(exp)
    print(report)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(report + "\n")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(to_json(exp), fh, indent=2)

    # Exit non-zero if a linear-case result was wrong: a result that cannot be
    # reproduced by full recomputation is not a result.
    linear_correct = all(t.correct for t in exp.sweep) and all(
        t.correct for t in exp.controls if not t.nonlinear
    )
    return 0 if linear_correct else 1


if __name__ == "__main__":
    sys.exit(main())
