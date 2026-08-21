"""Squeeze — a declarative footprint policy language for the platform.

Squeeze is code *about* code: a small language whose sources (`*.sqz`) declare
how much room the product's data is allowed to occupy — on disk, in memory, in
object storage, and in model weights — and which codec, tier and retention
window each class of data gets. The compiler turns a source into a **plan**: an
auditable footprint estimate with the drivers that produced it, the limits it
was checked against, and an explicit list of what it could not compute.

Why a language and not a config file:

  * **Units are typed.** `30 days` and `30 GiB` are different types and cannot be
    added. Most footprint spreadsheets are wrong in exactly this way.
  * **Rules are checked, not commented.** A retention window into an undeclared
    tier, a lossy codec on a clinical class, a cold window shorter than the warm
    one — all compile errors, not review findings.
  * **A plan explains itself.** Every plan ranks the drivers of its saving and
    they sum to the total exactly, the same contract the analytics engine holds
    its composite scores to.
  * **Declarations are verifiable.** `impl` names a real standard-library codec,
    and `squeeze verify` measures the ratio the source claims.

Nothing here compresses production data or applies a plan. The library is pure
— text in, data structures out — and the only I/O in the package is the CLI
reading a source file and writing a report. A plan is reviewed and applied by a
person, like the SQL in `database/migrations/`.

Layering: standard library only, no imports from the rest of this project —
the same rule `backend/algorithms/` lives under, enforced by `make lint-arch`.

    from backend.squeeze import compile_text, plan_policies

    program, diagnostics = compile_text(source)
    plans = plan_policies(program)

Command line:

    python -m backend.squeeze check  policies/footprint.sqz
    python -m backend.squeeze plan   policies/footprint.sqz [--json] [--policy NAME]
    python -m backend.squeeze verify policies/footprint.sqz

The grammar and every diagnostic code are specified in
`docs/23-squeeze-language.md`.
"""

from __future__ import annotations

from .checker import CURRENT_VERSION, compile_source
from .diagnostics import Diagnostic, DiagnosticBag, Severity, SqueezeError
from .model import Program
from .plan import Allocation, Driver, LimitCheck, Plan
from .planner import plan_policy, plan_program
from .report import format_bytes, plan_to_dict, render_drifts, render_plan
from .units import Dimension, Quantity
from .verify import Drift, Measurement, Sample, default_samples, measure, verify_program

__version__ = "0.1.0"

__all__ = [
    "CURRENT_VERSION",
    "Allocation",
    "Diagnostic",
    "DiagnosticBag",
    "Dimension",
    "Drift",
    "Driver",
    "LimitCheck",
    "Measurement",
    "Plan",
    "Program",
    "Quantity",
    "Sample",
    "Severity",
    "SqueezeError",
    "__version__",
    "compile_source",
    "compile_text",
    "default_samples",
    "format_bytes",
    "measure",
    "plan_policies",
    "plan_policy",
    "plan_program",
    "plan_to_dict",
    "render_drifts",
    "render_plan",
    "verify_program",
]


def compile_text(source: str) -> tuple[Program | None, DiagnosticBag]:
    """Compile a source string.

    Returns `(program, diagnostics)`. `program` is `None` when the source has
    errors; `diagnostics` is always worth reading, because warnings and
    unknowns are how the compiler reports a policy that is valid but not yet
    measurable.
    """
    diagnostics = DiagnosticBag()
    program = compile_source(source, diagnostics)
    return program, diagnostics


def plan_policies(program: Program) -> list[Plan]:
    """Compile every policy in a program to a plan, in declaration order."""
    return plan_program(program)
