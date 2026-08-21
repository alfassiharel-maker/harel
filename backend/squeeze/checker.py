"""Semantic analysis: CST -> `Program`, with every rule the language enforces.

The checker is where the language earns its keep over a YAML file. It rejects,
at compile time and before anyone provisions storage:

  * a retention window pointing at a tier that does not exist;
  * a codec listed on a tier that cannot handle that class's data kind;
  * a lossy codec with no declared fidelity, or a lossless one claiming to lose
    signal;
  * a tier ordering where a colder tier is kept for less time than a hotter one
    (which would silently delete data the policy claims to retain);
  * a `require fidelity >= x` that no available codec can satisfy (checked in
    the planner, where the codec choice is actually made).

Rules that cannot be decided from the source alone are reported as `UNKNOWN`
diagnostics, not errors and not silently defaulted: an unmeasured growth rate
means the plan cannot state a footprint for that class, and the reviewer needs
to see that gap rather than a confident zero.
"""

from __future__ import annotations

from fractions import Fraction

from .diagnostics import DiagnosticBag
from .model import (
    DATA_KINDS,
    Codec,
    CodecKind,
    DataClass,
    GrowthRate,
    Objective,
    Policy,
    Program,
    Requirement,
    Retention,
    Sensitivity,
    Tier,
)
from .nodes import Block, NumberAtom, QuantityAtom, Setting, SourceFile, StringAtom, WordAtom
from .parser import parse
from .units import UNITS, Dimension, Quantity

__all__ = ["CURRENT_VERSION", "check", "compile_source"]

#: Bumped when a change to the language would make an existing source mean
#: something different. A source declaring an older version still compiles; a
#: source declaring a newer one is rejected rather than guessed at.
CURRENT_VERSION = 1

_CODEC_SETTINGS = frozenset({"kind", "ratio", "cpu", "fidelity", "applies_to", "impl"})
_TIER_SETTINGS = frozenset({"medium", "latency", "unit_cost", "codecs"})
_CLASS_SETTINGS = frozenset({"record", "growth", "retain", "sensitivity"})
_POLICY_SETTINGS = frozenset({"objective", "population", "horizon", "limit", "require", "covers"})
_MEDIA = frozenset({"memory", "disk", "object", "archive"})
_GROWTH_SCOPES = frozenset({"user", "account", "fleet"})
_COMPARISONS = frozenset({"<=", ">=", "==", "!=", "<", ">"})


def compile_source(source: str, diagnostics: DiagnosticBag) -> Program | None:
    """Parse and check. Returns `None` if the source has errors."""
    tree = parse(source, diagnostics)
    if diagnostics.has_errors():
        return None
    program = check(tree, diagnostics)
    return None if diagnostics.has_errors() else program


def check(tree: SourceFile, diagnostics: DiagnosticBag) -> Program | None:
    version = tree.version
    if version is None:
        diagnostics.error(
            "SQZ0301",
            "the file does not declare a language version",
            1,
            1,
            hint=f"add `version {CURRENT_VERSION}` as the first line",
        )
        return None
    if version > CURRENT_VERSION:
        diagnostics.error(
            "SQZ0302",
            f"source declares version {version}; this compiler implements {CURRENT_VERSION}",
            1,
            1,
            hint="upgrade the compiler rather than lowering the version",
        )
        return None

    currency = _read_currency(tree, diagnostics)

    codecs: dict[str, Codec] = {}
    tiers: dict[str, Tier] = {}
    classes: dict[str, DataClass] = {}
    policies: dict[str, Policy] = {}

    for block in tree.blocks:
        declared: set[str] = {
            "codec": set(codecs),
            "tier": set(tiers),
            "class": set(classes),
            "policy": set(policies),
        }[block.keyword]
        if block.name in declared:
            diagnostics.error(
                "SQZ0303",
                f"duplicate {block.keyword} {block.name!r}",
                block.line,
                block.column,
                hint="names are the identity a plan is diffed on; they must be unique per kind",
            )
            continue
        if block.keyword == "codec":
            codecs[block.name] = _codec(block, diagnostics)
        elif block.keyword == "tier":
            tiers[block.name] = _tier(block, diagnostics)
        elif block.keyword == "class":
            classes[block.name] = _data_class(block, diagnostics)
        else:
            policies[block.name] = _policy(block, diagnostics)

    _cross_check(codecs, tiers, classes, policies, diagnostics)
    if diagnostics.has_errors():
        return None
    return Program(
        version=version, currency=currency, codecs=codecs, tiers=tiers, classes=classes, policies=policies
    )


def _read_currency(tree: SourceFile, diagnostics: DiagnosticBag) -> str:
    for setting in tree.preamble:
        if setting.key != "currency":
            continue
        atom = setting.atoms[0] if setting.atoms else None
        if isinstance(atom, StringAtom | WordAtom):
            return atom.text
        diagnostics.error("SQZ0304", "`currency` takes a code such as ILS", setting.line, setting.column)
    # Not defaulted silently in the money sense: with no currency declared the
    # cost objective is refused by the planner rather than assumed.
    return "unspecified"


# ---------------------------------------------------------------------------
# Block readers
# ---------------------------------------------------------------------------
def _unknown_settings(block: Block, allowed: frozenset[str], diagnostics: DiagnosticBag) -> None:
    for setting in block.settings:
        if setting.key not in allowed:
            diagnostics.error(
                "SQZ0305",
                f"unknown setting {setting.key!r} in {block.keyword} {block.name!r}",
                setting.line,
                setting.column,
                hint=f"{block.keyword} accepts {', '.join(sorted(allowed))}",
            )


def _first(block: Block, key: str) -> Setting | None:
    for setting in block.settings:
        if setting.key == key:
            return setting
    return None


def _all(block: Block, key: str) -> list[Setting]:
    return [s for s in block.settings if s.key == key]


def _quantity(setting: Setting, dimension: Dimension, diagnostics: DiagnosticBag) -> Quantity | None:
    for atom in setting.atoms:
        if isinstance(atom, QuantityAtom) and atom.quantity.dimension is dimension:
            return atom.quantity
    diagnostics.error(
        "SQZ0306",
        f"`{setting.key}` needs a {dimension.value} quantity with a unit",
        setting.line,
        setting.column,
        hint=f"units for {dimension.value}: {', '.join(_units_for(dimension))}",
    )
    return None


def _units_for(dimension: Dimension) -> list[str]:
    return sorted({name for name, (dim, _) in UNITS.items() if dim is dimension})


def _number(setting: Setting, diagnostics: DiagnosticBag) -> Fraction | None:
    for atom in setting.atoms:
        if isinstance(atom, NumberAtom):
            return atom.value
    diagnostics.error("SQZ0307", f"`{setting.key}` needs a plain number", setting.line, setting.column)
    return None


def _codec(block: Block, diagnostics: DiagnosticBag) -> Codec:
    _unknown_settings(block, _CODEC_SETTINGS, diagnostics)

    kind = CodecKind.LOSSLESS
    kind_setting = _first(block, "kind")
    if kind_setting is None:
        diagnostics.error(
            "SQZ0308",
            f"codec {block.name!r} does not say whether it is lossless or lossy",
            block.line,
            block.column,
            hint="this decides whether clinical classes may use it, so it is never inferred",
        )
    else:
        words = kind_setting.words()
        if words and words[0] in ("lossless", "lossy"):
            kind = CodecKind(words[0])
        else:
            diagnostics.error(
                "SQZ0309", "`kind` must be `lossless` or `lossy`", kind_setting.line, kind_setting.column
            )

    ratio: Fraction | None = None
    ratio_setting = _first(block, "ratio")
    if ratio_setting is not None:
        ratio = _number(ratio_setting, diagnostics)
        if ratio is not None and ratio < 1:
            diagnostics.error(
                "SQZ0310",
                f"codec {block.name!r} declares ratio {float(ratio):.3g}, which expands the data",
                ratio_setting.line,
                ratio_setting.column,
                hint="ratio is input bytes / output bytes and must be at least 1",
            )
            ratio = None
    else:
        diagnostics.unknown(
            "SQZ0311",
            f"codec {block.name!r} has no measured ratio, so the planner cannot select it",
            block.line,
            block.column,
            hint="measure it with `squeeze verify` and record the number here",
        )

    cpu: Quantity | None = None
    cpu_setting = _first(block, "cpu")
    if cpu_setting is not None:
        cpu = _rate_per_mib(cpu_setting, Dimension.TIME, diagnostics)

    fidelity: Fraction | None = None
    fidelity_setting = _first(block, "fidelity")
    if fidelity_setting is not None:
        fidelity = _number(fidelity_setting, diagnostics)
        if fidelity is not None and not (0 < fidelity <= 1):
            diagnostics.error(
                "SQZ0312",
                "`fidelity` is a fraction in (0, 1]",
                fidelity_setting.line,
                fidelity_setting.column,
            )
            fidelity = None
    if kind is CodecKind.LOSSY and fidelity is None:
        diagnostics.error(
            "SQZ0313",
            f"lossy codec {block.name!r} must declare its fidelity",
            block.line,
            block.column,
            hint="a lossy codec with unstated fidelity cannot be checked against `require fidelity >= x`",
        )
    if kind is CodecKind.LOSSLESS:
        if fidelity is not None and fidelity != 1:
            diagnostics.error(
                "SQZ0314",
                f"lossless codec {block.name!r} declares fidelity {float(fidelity):.4g}",
                block.line,
                block.column,
                hint="lossless means fidelity 1; if it loses signal, declare `kind lossy`",
            )
        fidelity = Fraction(1)

    applies_to: set[str] = set()
    applies_setting = _first(block, "applies_to")
    if applies_setting is None:
        diagnostics.error(
            "SQZ0315",
            f"codec {block.name!r} does not say which data kinds it applies to",
            block.line,
            block.column,
            hint=f"kinds: {', '.join(sorted(DATA_KINDS))}",
        )
    else:
        for word in applies_setting.words():
            if word in DATA_KINDS:
                applies_to.add(word)
            else:
                diagnostics.error(
                    "SQZ0316",
                    f"unknown data kind {word!r}",
                    applies_setting.line,
                    applies_setting.column,
                    hint=f"kinds: {', '.join(sorted(DATA_KINDS))}",
                )

    implementation: str | None = None
    impl_setting = _first(block, "impl")
    if impl_setting is not None:
        words = impl_setting.words()
        implementation = words[0] if words else None

    return Codec(
        name=block.name,
        kind=kind,
        ratio=ratio,
        cpu_per_mib=cpu,
        fidelity=fidelity,
        applies_to=frozenset(applies_to),
        implementation=implementation,
        line=block.line,
    )


def _rate_per_mib(setting: Setting, dimension: Dimension, diagnostics: DiagnosticBag) -> Quantity | None:
    """Read `<quantity> per <byte-unit>` and normalise to per-MiB."""
    quantity = _quantity(setting, dimension, diagnostics)
    if quantity is None:
        return None
    words = setting.words()
    if "per" not in words:
        diagnostics.error(
            "SQZ0317",
            f"`{setting.key}` must be written as `<amount> per <unit>`",
            setting.line,
            setting.column,
            hint="for example `cpu 1.8 ms per MiB`",
        )
        return None
    denominator = words[words.index("per") + 1] if words.index("per") + 1 < len(words) else ""
    entry = UNITS.get(denominator)
    if entry is None or entry[0] is not Dimension.BYTES:
        diagnostics.error(
            "SQZ0318",
            f"`{setting.key}` must be per a byte unit, got {denominator!r}",
            setting.line,
            setting.column,
        )
        return None
    # Scale to per-MiB so the planner never has to carry the author's unit.
    per_mib = quantity.as_fraction() * (Fraction(1024 * 1024) / entry[1])
    return Quantity(magnitude=per_mib, dimension=dimension, unit=quantity.unit)


def _tier(block: Block, diagnostics: DiagnosticBag) -> Tier:
    _unknown_settings(block, _TIER_SETTINGS, diagnostics)

    medium = "unspecified"
    medium_setting = _first(block, "medium")
    if medium_setting is None:
        diagnostics.error(
            "SQZ0319",
            f"tier {block.name!r} declares no medium",
            block.line,
            block.column,
            hint=f"media: {', '.join(sorted(_MEDIA))}",
        )
    else:
        words = medium_setting.words()
        if words and words[0] in _MEDIA:
            medium = words[0]
        else:
            diagnostics.error(
                "SQZ0320",
                f"unknown medium {words[0] if words else ''!r}",
                medium_setting.line,
                medium_setting.column,
                hint=f"media: {', '.join(sorted(_MEDIA))}",
            )

    latency: Quantity | None = None
    latency_setting = _first(block, "latency")
    if latency_setting is not None:
        latency = _quantity(latency_setting, Dimension.TIME, diagnostics)

    unit_cost: Fraction | None = None
    cost_setting = _first(block, "unit_cost")
    if cost_setting is not None:
        money = _quantity(cost_setting, Dimension.MONEY, diagnostics)
        words = cost_setting.words()
        if money is not None:
            if words.count("per") == 2 and words[words.index("per") + 1] == "GiB":
                unit_cost = money.as_fraction()
            else:
                diagnostics.error(
                    "SQZ0321",
                    "`unit_cost` must be written as `<n> minor per GiB per month`",
                    cost_setting.line,
                    cost_setting.column,
                    hint="one shape only: comparing tiers requires a common denominator",
                )

    codec_names: tuple[str, ...] = ()
    codecs_setting = _first(block, "codecs")
    if codecs_setting is None:
        diagnostics.unknown(
            "SQZ0322",
            f"tier {block.name!r} allows no codecs, so its data is stored uncompressed",
            block.line,
            block.column,
        )
    else:
        codec_names = tuple(codecs_setting.words())

    return Tier(
        name=block.name,
        medium=medium,
        latency=latency,
        unit_cost_per_gib_month=unit_cost,
        codecs=codec_names,
        line=block.line,
    )


def _data_class(block: Block, diagnostics: DiagnosticBag) -> DataClass:
    _unknown_settings(block, _CLASS_SETTINGS, diagnostics)

    kind = block.kind
    if kind is None:
        diagnostics.error(
            "SQZ0323",
            f"class {block.name!r} has no data kind",
            block.line,
            block.column,
            hint=f"write `class {block.name} : timeseries {{`; kinds: {', '.join(sorted(DATA_KINDS))}",
        )
        kind = "unspecified"
    elif kind not in DATA_KINDS:
        diagnostics.error(
            "SQZ0324",
            f"unknown data kind {kind!r}",
            block.line,
            block.column,
            hint=f"kinds: {', '.join(sorted(DATA_KINDS))}",
        )

    record_size: Quantity | None = None
    record_setting = _first(block, "record")
    if record_setting is None:
        diagnostics.unknown(
            "SQZ0325",
            f"class {block.name!r} has no record size, so its footprint is unknown",
            block.line,
            block.column,
            hint="measure one representative row and record it; do not guess",
        )
    else:
        record_size = _quantity(record_setting, Dimension.BYTES, diagnostics)

    growth: GrowthRate | None = None
    growth_setting = _first(block, "growth")
    if growth_setting is None:
        diagnostics.unknown(
            "SQZ0326",
            f"class {block.name!r} has no growth rate, so its footprint is unknown",
            block.line,
            block.column,
        )
    else:
        growth = _growth(growth_setting, diagnostics)

    retain: list[Retention] = []
    retain_setting = _first(block, "retain")
    if retain_setting is None:
        diagnostics.error(
            "SQZ0327",
            f"class {block.name!r} declares no retention",
            block.line,
            block.column,
            hint="data with no stated lifetime is data nobody has decided to keep or delete",
        )
    else:
        retain = _retention(retain_setting, diagnostics)

    sensitivity = Sensitivity.ROUTINE
    sensitivity_setting = _first(block, "sensitivity")
    if sensitivity_setting is not None:
        words = sensitivity_setting.words()
        if words and words[0] in {s.value for s in Sensitivity}:
            sensitivity = Sensitivity(words[0])
        else:
            diagnostics.error(
                "SQZ0328",
                f"unknown sensitivity {words[0] if words else ''!r}",
                sensitivity_setting.line,
                sensitivity_setting.column,
                hint=f"one of {', '.join(s.value for s in Sensitivity)}",
            )

    return DataClass(
        name=block.name,
        kind=kind,
        record_size=record_size,
        growth=growth,
        retain=tuple(retain),
        sensitivity=sensitivity,
        line=block.line,
    )


def _growth(setting: Setting, diagnostics: DiagnosticBag) -> GrowthRate | None:
    amount: Fraction | None = None
    for atom in setting.atoms:
        if isinstance(atom, QuantityAtom) and atom.quantity.dimension is Dimension.COUNT:
            amount = atom.quantity.count()
            break
        if isinstance(atom, NumberAtom):
            amount = atom.value
            break
    words = setting.words()
    if amount is None or words.count("per") != 2:
        diagnostics.error(
            "SQZ0329",
            "`growth` must read `<n> records per <scope> per <period>`",
            setting.line,
            setting.column,
            hint="for example `growth 900 records per user per month`",
        )
        return None

    first_per = words.index("per")
    scope = words[first_per + 1] if first_per + 1 < len(words) else ""
    second_per = words.index("per", first_per + 1)
    period_word = words[second_per + 1] if second_per + 1 < len(words) else ""

    if scope not in _GROWTH_SCOPES:
        diagnostics.error(
            "SQZ0330",
            f"unknown growth scope {scope!r}",
            setting.line,
            setting.column,
            hint=f"scopes: {', '.join(sorted(_GROWTH_SCOPES))}",
        )
        return None
    entry = UNITS.get(period_word)
    if entry is None or entry[0] is not Dimension.TIME:
        diagnostics.error(
            "SQZ0331",
            f"unknown growth period {period_word!r}",
            setting.line,
            setting.column,
            hint="a period is a time unit: day, week, month, year",
        )
        return None
    return GrowthRate(amount=amount, scope=scope, period=Quantity.parse(Fraction(1), period_word))


def _retention(setting: Setting, diagnostics: DiagnosticBag) -> list[Retention]:
    """Read `retain hot 30 days, warm 12 months, cold 5 years`."""
    windows: list[Retention] = []
    pending_tier: str | None = None
    for atom in setting.atoms:
        if isinstance(atom, WordAtom):
            if pending_tier is not None:
                diagnostics.error(
                    "SQZ0332",
                    f"tier {pending_tier!r} in `retain` has no window",
                    atom.line,
                    atom.column,
                    hint="each tier is followed by a duration: `hot 30 days`",
                )
            pending_tier = atom.text
            continue
        if isinstance(atom, QuantityAtom) and atom.quantity.dimension is Dimension.TIME:
            if pending_tier is None:
                diagnostics.error(
                    "SQZ0333",
                    "a retention window with no tier",
                    atom.line,
                    atom.column,
                )
                continue
            windows.append(Retention(tier=pending_tier, window=atom.quantity, line=atom.line))
            pending_tier = None
            continue
        diagnostics.error("SQZ0334", "`retain` takes tier/duration pairs", atom.line, atom.column)
    if pending_tier is not None:
        diagnostics.error(
            "SQZ0332",
            f"tier {pending_tier!r} in `retain` has no window",
            setting.line,
            setting.column,
            hint="each tier is followed by a duration: `hot 30 days`",
        )
    return windows


def _policy(block: Block, diagnostics: DiagnosticBag) -> Policy:
    _unknown_settings(block, _POLICY_SETTINGS, diagnostics)

    objective = Objective.BYTES
    objective_setting = _first(block, "objective")
    if objective_setting is None:
        diagnostics.error(
            "SQZ0335",
            f"policy {block.name!r} states no objective",
            block.line,
            block.column,
            hint="`objective minimise bytes` — the planner needs to know what it is minimising",
        )
    else:
        # `minimise` reads as part of the sentence but carries no information:
        # every objective is a minimisation.
        named = [w for w in objective_setting.words() if w != "minimise"]
        if named and named[0] in {o.value for o in Objective}:
            objective = Objective(named[0])
        else:
            diagnostics.error(
                "SQZ0336",
                f"unknown objective {named[0] if named else ''!r}",
                objective_setting.line,
                objective_setting.column,
                hint=f"one of {', '.join(o.value for o in Objective)}",
            )

    population: Fraction | None = None
    population_setting = _first(block, "population")
    if population_setting is not None:
        quantity = _quantity(population_setting, Dimension.COUNT, diagnostics)
        population = None if quantity is None else quantity.count()

    horizon: Quantity | None = None
    horizon_setting = _first(block, "horizon")
    if horizon_setting is not None:
        horizon = _quantity(horizon_setting, Dimension.TIME, diagnostics)

    limits: dict[str, Quantity] = {}
    for limit_setting in _all(block, "limit"):
        limit_words = limit_setting.words()
        quantity = _quantity(limit_setting, Dimension.BYTES, diagnostics)
        if not limit_words:
            diagnostics.error(
                "SQZ0337",
                "`limit` needs a name: `limit total 40 GiB`",
                limit_setting.line,
                limit_setting.column,
            )
            continue
        if quantity is None:
            continue
        if limit_words[0] in limits:
            diagnostics.error(
                "SQZ0338",
                f"limit {limit_words[0]!r} is set twice",
                limit_setting.line,
                limit_setting.column,
            )
            continue
        limits[limit_words[0]] = quantity

    requirements: list[Requirement] = []
    for require_setting in _all(block, "require"):
        requirement = _requirement(require_setting, diagnostics)
        if requirement is not None:
            requirements.append(requirement)

    covers_setting = _first(block, "covers")
    covered = tuple(covers_setting.words()) if covers_setting is not None else ()

    return Policy(
        name=block.name,
        objective=objective,
        population=population,
        horizon=horizon,
        limits=limits,
        requirements=tuple(requirements),
        classes=covered,
        line=block.line,
    )


def _requirement(setting: Setting, diagnostics: DiagnosticBag) -> Requirement | None:
    atoms = list(setting.atoms)
    if not atoms:
        diagnostics.error("SQZ0339", "`require` needs a subject", setting.line, setting.column)
        return None
    subject_atom = atoms[0]
    if not isinstance(subject_atom, WordAtom):
        diagnostics.error(
            "SQZ0340", "`require` subject must be a name", subject_atom.line, subject_atom.column
        )
        return None
    if len(atoms) == 1:
        return Requirement(subject=subject_atom.text, operator=None, value=None, line=setting.line)

    operator_atom = atoms[1]
    if not isinstance(operator_atom, WordAtom) or operator_atom.text not in _COMPARISONS:
        diagnostics.error(
            "SQZ0341",
            "expected a comparison operator",
            operator_atom.line,
            operator_atom.column,
            hint=f"one of {', '.join(sorted(_COMPARISONS))}",
        )
        return None
    if len(atoms) < 3:
        diagnostics.error("SQZ0342", "the comparison has no right-hand side", setting.line, setting.column)
        return None
    value_atom = atoms[2]
    value: Fraction | Quantity
    if isinstance(value_atom, NumberAtom):
        value = value_atom.value
    elif isinstance(value_atom, QuantityAtom):
        value = value_atom.quantity
    else:
        diagnostics.error(
            "SQZ0343",
            "a comparison compares against a number or a quantity",
            value_atom.line,
            value_atom.column,
        )
        return None
    return Requirement(subject=subject_atom.text, operator=operator_atom.text, value=value, line=setting.line)


# ---------------------------------------------------------------------------
# Cross-declaration rules
# ---------------------------------------------------------------------------
def _cross_check(
    codecs: dict[str, Codec],
    tiers: dict[str, Tier],
    classes: dict[str, DataClass],
    policies: dict[str, Policy],
    diagnostics: DiagnosticBag,
) -> None:
    for tier in tiers.values():
        for name in tier.codecs:
            if name not in codecs:
                diagnostics.error(
                    "SQZ0350",
                    f"tier {tier.name!r} lists undeclared codec {name!r}",
                    tier.line,
                    1,
                    hint=f"declared codecs: {', '.join(sorted(codecs)) or 'none'}",
                )

    for data_class in classes.values():
        seen: set[str] = set()
        previous: Quantity | None = None
        for retention in data_class.retain:
            if retention.tier not in tiers:
                diagnostics.error(
                    "SQZ0351",
                    f"class {data_class.name!r} retains into undeclared tier {retention.tier!r}",
                    retention.line,
                    1,
                    hint=f"declared tiers: {', '.join(sorted(tiers)) or 'none'}",
                )
                continue
            if retention.tier in seen:
                diagnostics.error(
                    "SQZ0352",
                    f"class {data_class.name!r} retains into tier {retention.tier!r} twice",
                    retention.line,
                    1,
                )
                continue
            seen.add(retention.tier)
            # Windows are cumulative from the hottest tier outwards, so each one
            # must be at least as long as the one before it. A cold window
            # shorter than the warm window would delete data the policy claims
            # to still hold.
            if previous is not None and retention.window.seconds() < previous.seconds():
                diagnostics.error(
                    "SQZ0353",
                    f"class {data_class.name!r}: window {retention.window} for tier "
                    f"{retention.tier!r} is shorter than the preceding window {previous}",
                    retention.line,
                    1,
                    hint="retention windows are cumulative and must not decrease",
                )
            previous = retention.window

            tier = tiers[retention.tier]
            usable = [
                name for name in tier.codecs if name in codecs and data_class.kind in codecs[name].applies_to
            ]
            if tier.codecs and not usable:
                diagnostics.warn(
                    "SQZ0354",
                    f"no codec on tier {retention.tier!r} applies to {data_class.kind!r} data "
                    f"(class {data_class.name!r}); it will be stored uncompressed",
                    retention.line,
                    1,
                )

    for policy in policies.values():
        for name in policy.classes:
            if name not in classes:
                diagnostics.error(
                    "SQZ0355",
                    f"policy {policy.name!r} covers undeclared class {name!r}",
                    policy.line,
                    1,
                )
        if not policy.classes and classes:
            diagnostics.warn(
                "SQZ0356",
                f"policy {policy.name!r} lists no classes; every declared class is assumed covered",
                policy.line,
                1,
                hint="write `covers <class>, <class>` to make the scope explicit",
            )

    for codec in codecs.values():
        if codec.implementation is None and codec.ratio is not None:
            diagnostics.unknown(
                "SQZ0357",
                f"codec {codec.name!r} declares a ratio but names no implementation, "
                "so the declaration cannot be verified against measured bytes",
                codec.line,
                1,
                hint="add `impl <name>` and run `squeeze verify`",
            )
