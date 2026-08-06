"""The two prompts and the JSON schemas that constrain their replies.

Split into two calls because the second must reason *over* a finished map. Asked
for a map and its conclusions in one breath, a model produces conclusions shaped
like the map — restatements rather than inferences.

The schemas cannot express the things that actually matter here: minimum
lengths, non-empty buckets, absence of hedging. Structured outputs support none
of those constraints. So the schema guarantees shape only, and every rule with
teeth lives in :mod:`scout.models`, which sees the payload before anything else
does. The prompt states the rules so the model can meet them; the validator is
what makes them true.
"""

from __future__ import annotations

from typing import Any, Final

_STRINGS: Final[dict[str, Any]] = {"type": "array", "items": {"type": "string"}}


def _obj(**properties: Any) -> dict[str, Any]:
    """A closed object with every property required.

    Structured outputs demand ``additionalProperties: false`` and an explicit
    ``required`` list, and there is no field here we would accept as optional —
    an omitted field is exactly the shallowness we are guarding against.
    """
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


# --------------------------------------------------------------------------- #
# call 1 — the domain map
# --------------------------------------------------------------------------- #

ANALYSIS_SCHEMA: Final[dict[str, Any]] = _obj(
    domain={"type": "string"},
    structure=_obj(
        architecture_notes=_STRINGS,
        components={
            "type": "array",
            "items": _obj(name={"type": "string"}, role={"type": "string"}),
        },
        data_flows={
            "type": "array",
            "items": _obj(
                source={"type": "string"},
                target={"type": "string"},
                payload={"type": "string"},
            ),
        },
    ),
    engineering=_obj(
        processes=_STRINGS,
        bottlenecks=_STRINGS,
        waste=_STRINGS,
        inefficiencies=_STRINGS,
    ),
    business=_obj(
        users=_STRINGS,
        customers=_STRINGS,
        spend_areas=_STRINGS,
        high_costs=_STRINGS,
        improvable_processes=_STRINGS,
    ),
)

ANALYSIS_SYSTEM: Final = """\
You map how a technical or business domain actually works today, for a founder \
deciding where to spend the next two years.

Describe the domain as a practitioner inside it would: the components that \
exist, what moves between them, and how the work is really performed — including \
the parts done by hand, by convention, or by someone remembering to. Name \
concrete mechanisms rather than categories.

Two rules about substance:

- A component or a process is worth listing only if you can say what it does and \
  what it costs someone. "Uses the cloud" describes nothing; "reserves \
  accelerator capacity for peak concurrency and bills by wall-clock hour" \
  describes a mechanism with a price attached.
- Bottlenecks, waste and inefficiency are three different things, and the answer \
  is more useful when they stay separate. A bottleneck limits throughput. Waste \
  is a resource paid for and not used. An inefficiency is work that could be \
  done a better way but currently is not.

You have no access to sources, so state domain knowledge plainly and do not \
present statistics as measured facts. Write all content in Hebrew. Field names \
stay in English.\
"""


def analysis_prompt(domain: str) -> str:
    return (
        f"Map this domain: {domain}\n\n"
        "Cover, in the shape the schema requires:\n"
        "1. Structure — how systems in this domain are architected, which "
        "components exist, and what data moves between which components.\n"
        "2. Engineering — the processes as they are performed today, where "
        "throughput is limited, where resources are paid for and not used, and "
        "what is still done in an inefficient way.\n"
        "3. Business — who the users are, who actually pays, where the money "
        "goes, which costs are large, and which processes could improve.\n"
    )


# --------------------------------------------------------------------------- #
# call 2 — problems derived from the map
# --------------------------------------------------------------------------- #

_DIMENSIONS: Final[dict[str, Any]] = _obj(
    severity={"type": "integer"},
    cost={"type": "integer"},
    breadth={"type": "integer"},
    solution_maturity={"type": "integer"},
    startup_potential={"type": "integer"},
)

_NOTES: Final[dict[str, Any]] = _obj(
    severity={"type": "string"},
    cost={"type": "string"},
    breadth={"type": "string"},
    solution_maturity={"type": "string"},
    startup_potential={"type": "string"},
)

PROBLEMS_SCHEMA: Final[dict[str, Any]] = _obj(
    problems={
        "type": "array",
        "items": _obj(
            id={"type": "string"},
            title={"type": "string"},
            mechanism={"type": "string"},
            who_pays={"type": "string"},
            current_workaround={"type": "string"},
            signal={"type": "string"},
            confidence={"type": "string", "enum": ["high", "medium", "low"]},
            known_facts=_STRINGS,
            hypotheses=_STRINGS,
            open_questions=_STRINGS,
            dimensions=_DIMENSIONS,
            dimension_notes=_NOTES,
        ),
    }
)

PROBLEMS_SYSTEM: Final = """\
You find the gap between how a domain works today and how it could work, given a \
finished map of that domain.

Every problem you return is derived from something in the map. You are not \
brainstorming; you are pointing at a specific place where money, time or \
attention leaks, and saying why it leaks there.

The bar, illustrated. Rejected: "an AI company uses the cloud." Accepted: "a \
company runs large models on GPUs for long stretches and pays for capacity while \
utilisation is partial, because the pool is sized for peak concurrency and job \
arrival is bursty — is there an opportunity to make that gap visible and \
recoverable?" The difference is a causal mechanism and a named payer.

Each problem carries five fields that make it checkable rather than plausible:

- mechanism: why this happens, causally. Not a restatement of the title. If you \
  cannot name the cause, you have found a symptom and should look for the cause \
  instead.
- who_pays: the specific role or company type that carries the cost.
- current_workaround: what people do today instead. A problem nobody works \
  around is a problem nobody has.
- signal: something observable that a reader can go and check for themselves, \
  using data they could plausibly obtain. This is the field that separates a real \
  gap from a story about one.
- confidence: high, medium or low. Low is a useful answer.

Separate your claims into three buckets and never mix them:

- known_facts: established domain knowledge, stated flatly. No hedging words. If \
  a sentence needs "probably", "likely" or "it seems", it is not a fact and \
  belongs in hypotheses. Each bucket must contain at least one entry.
- hypotheses: the analytical leap — where you assert that the gap is large \
  enough, or persistent enough, to matter. Hedged language is correct here.
- open_questions: what a reader must verify before acting. If nothing needs \
  verifying, you have invented the problem rather than found it.

Score each problem 0-5 on five dimensions and justify each score in one \
sentence: severity (how badly it hurts), cost (money or time burned today), \
breadth (how many people or companies), solution_maturity (how well existing \
solutions already cover it — a high value counts *against* the opportunity), and \
startup_potential (whether this can be a company, which is a separate question \
from whether it is a real problem: regulated markets, incumbent-owned ground and \
features-not-products all score low here).

You have no access to sources. Do not present any number as measured. Write all \
content in Hebrew; field names, ids and the confidence value stay in English.\
"""


def problems_prompt(analysis_json: str, count: int) -> str:
    return (
        f"Here is the domain map:\n\n{analysis_json}\n\n"
        f"Return {count} candidate problems derived from it, ordered however you "
        "like — they will be scored and ranked mechanically afterwards.\n\n"
        "Draw on the whole map, not only the sections named 'waste' and "
        "'bottlenecks'. Some of the best candidates sit where an engineering fact "
        "meets a business one: a manual process next to a large cost, or a "
        "component whose owner cannot see the price of its own decisions.\n\n"
        "Give each problem a short lowercase-hyphenated id."
    )
