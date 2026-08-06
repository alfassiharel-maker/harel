"""The provider boundary — the only module in this package that touches a network.

Nothing above this file knows which provider answered. A different provider is a
new class implementing :class:`LanguageModel` and no other change.

The key is read from the environment and from nowhere else: not a flag, not a
config file, and it is never written into a run record, a log line, or an
exception message.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Final, Mapping, Protocol, Sequence

#: Anthropic list pricing for claude-opus-5, in micro-dollars per token:
#: $5 per million input tokens and $25 per million output tokens. Thinking
#: tokens bill as output, which is why a run's output count can exceed the
#: visible JSON by a wide margin.
INPUT_MICROS_PER_TOKEN: Final = 5
OUTPUT_MICROS_PER_TOKEN: Final = 25

DEFAULT_MODEL: Final = "claude-opus-5"

#: Adaptive thinking is on by default on this model and its tokens count against
#: ``max_tokens``, so the ceiling has to cover reasoning *and* the JSON payload.
#: A domain map or a problem list that truncates mid-object is unparseable, so
#: the budget is generous and the call streams to stay under HTTP timeouts.
DEFAULT_MAX_TOKENS: Final = 32_000

#: Reasoning depth. ``high`` is the floor for intelligence-sensitive work, which
#: this is: the whole point of the second call is a non-obvious inference.
DEFAULT_EFFORT: Final = "high"


class ConfigurationError(RuntimeError):
    """The model cannot be reached because of how the run is configured."""


class ModelCallError(RuntimeError):
    """The provider answered, but the answer is not usable."""


@dataclass(frozen=True, slots=True)
class ModelReply:
    payload: object
    input_tokens: int | None
    output_tokens: int | None


class LanguageModel(Protocol):
    """One method: a prompt in, parsed JSON out."""

    provider: str
    model: str

    def complete_json(
        self,
        *,
        system: str,
        prompt: str,
        schema: Mapping[str, Any],
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> ModelReply: ...


def cost_micros(input_tokens: int | None, output_tokens: int | None) -> int | None:
    """Cost in micro-dollars, or ``None`` when the provider reported no usage.

    Integer minor units throughout — a float would round money.
    """
    if input_tokens is None or output_tokens is None:
        return None
    return input_tokens * INPUT_MICROS_PER_TOKEN + output_tokens * OUTPUT_MICROS_PER_TOKEN


# --------------------------------------------------------------------------- #
# offline
# --------------------------------------------------------------------------- #


class MockModel:
    """Replays recorded payloads in order. No network, no key, no dependencies.

    This is what lets the whole pipeline be exercised offline, and it is the only
    model the test suite uses.
    """

    provider = "mock"

    def __init__(self, payloads: Sequence[object], model: str = "fixture") -> None:
        self.model = model
        self._payloads = list(payloads)
        self._calls = 0

    @property
    def calls(self) -> int:
        return self._calls

    def complete_json(
        self,
        *,
        system: str,
        prompt: str,
        schema: Mapping[str, Any],
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> ModelReply:
        if self._calls >= len(self._payloads):
            raise ModelCallError(
                f"mock model exhausted after {len(self._payloads)} replies"
            )
        payload = self._payloads[self._calls]
        self._calls += 1
        return ModelReply(payload=payload, input_tokens=None, output_tokens=None)


# --------------------------------------------------------------------------- #
# anthropic
# --------------------------------------------------------------------------- #


class AnthropicModel:
    """Anthropic Messages API, constrained to JSON output.

    The SDK is imported lazily so that steps that do not call a model — the
    scoring layer, the validator, the renderer and their tests — keep running on
    a machine with nothing installed.
    """

    provider = "anthropic"

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        effort: str = DEFAULT_EFFORT,
    ) -> None:
        try:
            import anthropic
        except ModuleNotFoundError as exc:  # pragma: no cover - environment
            raise ConfigurationError(
                "the anthropic package is not installed; `pip install anthropic`"
            ) from exc

        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise ConfigurationError(
                "ANTHROPIC_API_KEY is not set in the environment"
            )

        self.model = model
        self.effort = effort
        # The client reads the key from the environment itself; it is never held
        # in an attribute here, so it cannot be reached from a run record.
        self._client = anthropic.Anthropic()

    def complete_json(
        self,
        *,
        system: str,
        prompt: str,
        schema: Mapping[str, Any],
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> ModelReply:
        # Streaming, because max_tokens is large enough that a non-streaming
        # request risks an idle-connection timeout. No temperature or top_p —
        # this model rejects them. Thinking is adaptive by default; depth is
        # output_config.effort.
        with self._client.messages.stream(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            output_config={
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": dict(schema)},
            },
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            message = stream.get_final_message()

        # Check why generation stopped before reading any content: on a refusal
        # the content list may be empty, and on max_tokens the JSON is truncated
        # and parsing it would yield a confusing error far from the cause.
        if message.stop_reason == "refusal":
            category = getattr(message.stop_details, "category", None)
            raise ModelCallError(f"the model declined the request (category: {category})")
        if message.stop_reason == "max_tokens":
            raise ModelCallError(
                f"the reply hit the {max_tokens} token ceiling and is truncated; "
                "raise --max-tokens or lower --effort"
            )

        text = next((block.text for block in message.content if block.type == "text"), None)
        if text is None:
            raise ModelCallError(
                f"no text block in the reply (stop_reason: {message.stop_reason})"
            )

        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ModelCallError(f"the reply is not valid JSON: {exc}") from exc

        usage = message.usage
        return ModelReply(
            payload=payload,
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
        )
