"""Token-usage accounting for Security Watch runs.

Two independent, separately billed surfaces are measured:

* the outer agent loop (mistral-large-4), from the public ``post_llm_call`` hook;
* the hosted search bridge (Small 4), from ``ConversationResponse.usage``.

They use separate verified rate cards, are reported separately, and are added
only for the partial model-token estimate. Nothing here enforces a budget or
claims to be a provider invoice.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .search import HOSTED_SEARCH_MODEL
from .sdk_compat import prepare_sdk_environment

prepare_sdk_environment()

from mistralai import agents  # noqa: E402
from pydantic import PrivateAttr  # noqa: E402

PRICING_SOURCE = "https://docs.mistral.ai/inference/pricing.md"
PRICING_AS_OF = "2026-11-15"


@dataclass(frozen=True)
class RateCard:
    """One verified model rate card. Rates are USD per million tokens."""

    model: str
    input_per_million_usd: float
    cached_input_per_million_usd: float | None
    output_per_million_usd: float
    cached_input_pricing: str
    source: str = PRICING_SOURCE
    as_of: str = PRICING_AS_OF

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "input_per_million_usd": self.input_per_million_usd,
            "cached_input_per_million_usd": self.cached_input_per_million_usd,
            "output_per_million_usd": self.output_per_million_usd,
            "cached_input_pricing": self.cached_input_pricing,
            "source": self.source,
            "as_of": self.as_of,
        }


# The outer model must match security_watch.agent_factory.MODEL.
OUTER_RATE_CARD = RateCard(
    model="mistral-large-4",
    input_per_million_usd=0.70,
    cached_input_per_million_usd=0.07,
    output_per_million_usd=2.10,
    cached_input_pricing="verified published cached-input rate",
    source="https://docs.mistral.ai/models/mistral-large-4-0.md",
)

SEARCH_RATE_CARD = RateCard(
    model=HOSTED_SEARCH_MODEL,
    input_per_million_usd=0.15,
    cached_input_per_million_usd=None,
    output_per_million_usd=0.60,
    cached_input_pricing=(
        "no verified cached-input rate; Conversations reports no cached tokens, "
        "so reported prompt tokens are priced at the standard input rate"
    ),
)

CAVEATS = (
    "Partial model-token estimate only; it is not a bill or invoice.",
    "Provider attempts that failed before a candidate was reported, hidden "
    "compaction calls, and usage the SDK did not report are excluded.",
    "Connector/search fees are unverified and excluded. connector_tokens is "
    "reported but not priced; it is not assumed to be zero.",
    "connectors is a raw provider map; its values are provider-defined and may "
    "be call counts, not tokens.",
    "Conversations total_tokens is provider-reported and may include connector "
    "tokens; it need not equal prompt + completion.",
    "cache_write_tokens has no supplied rate; it is counted but not priced "
    "separately.",
    "Small 4 cached-input pricing is not verified. Conversations reports no "
    "cached-token data, so reported search prompt tokens are priced at the "
    "standard input rate; the estimate may differ from the invoice if caching "
    "applies.",
)


def _as_int(value: object) -> int | None:
    if value is None or type(value).__name__ == "Unset":
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _field(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _first_int(value: object, names: tuple[str, ...]) -> int | None:
    for name in names:
        parsed = _as_int(_field(value, name))
        if parsed is not None:
            return parsed
    return None


@dataclass
class ModelUsage:
    """Accumulated usage for one surface. Tokens are reported, never derived."""

    calls: int = 0
    calls_without_usage: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens_reported: int = 0

    def add(self, usage: object | None) -> None:
        self.calls += 1
        if usage is None:
            self.calls_without_usage += 1
            return
        input_tokens = _first_int(usage, ("input_tokens", "prompt_tokens"))
        output_tokens = _first_int(usage, ("output_tokens", "completion_tokens"))
        if input_tokens is None and output_tokens is None:
            self.calls_without_usage += 1
            return
        self.input_tokens += input_tokens or 0
        self.output_tokens += output_tokens or 0
        self.cached_input_tokens += _as_int(_field(usage, "cached_input_tokens")) or 0
        self.cache_write_tokens += _as_int(_field(usage, "cache_write_tokens")) or 0
        self.reasoning_tokens += _as_int(_field(usage, "reasoning_tokens")) or 0
        self.total_tokens_reported += _as_int(_field(usage, "total_tokens")) or 0

    def as_dict(self) -> dict[str, int]:
        return {
            "calls": self.calls,
            "calls_without_usage": self.calls_without_usage,
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens_reported": self.total_tokens_reported,
        }


def estimate_model_tokens_usd(usage: ModelUsage, rate_card: RateCard) -> float:
    """Estimate USD from prompt/completion tokens using one rate card.

    ``cached_input_tokens`` is a subset of ``input_tokens``. When the rate card
    has a verified cached-input rate, the subset is priced at that rate. When it
    does not, all reported prompt tokens are priced at the standard input rate.
    ``reasoning_tokens`` is a subset of ``output_tokens`` and is not priced
    separately. Connector tokens have no verified rate and are excluded.
    """

    if rate_card.cached_input_per_million_usd is None:
        amount = (
            usage.input_tokens * rate_card.input_per_million_usd
            + usage.output_tokens * rate_card.output_per_million_usd
        ) / 1_000_000
    else:
        uncached = max(0, usage.input_tokens - usage.cached_input_tokens)
        amount = (
            uncached * rate_card.input_per_million_usd
            + usage.cached_input_tokens * rate_card.cached_input_per_million_usd
            + usage.output_tokens * rate_card.output_per_million_usd
        ) / 1_000_000
    return round(amount, 6)


def _count_tool_executions(response: object) -> int:
    outputs = _field(response, "outputs")
    if not isinstance(outputs, (list, tuple)):
        return 0
    return sum(1 for entry in outputs if _field(entry, "type") == "tool.execution")


@dataclass
class UsageMeter:
    """Thread-safe accumulator for both billable surfaces of one run."""

    outer: ModelUsage = field(default_factory=ModelUsage)
    search: ModelUsage = field(default_factory=ModelUsage)
    # Models observed on search responses. Empty means no response reported a
    # model; anything other than the default means search pricing is unknown.
    search_models: set[str] = field(default_factory=set)
    final_turn: ModelUsage = field(default_factory=ModelUsage)
    search_calls: int = 0
    search_failures: int = 0
    search_cancellations: int = 0
    search_unknown_billed_usage_calls: int = 0
    hosted_tool_executions: int = 0
    connector_tokens_reported: int = 0
    connectors_reported: dict[str, int] = field(default_factory=dict)
    connector_metadata_responses: int = 0
    _lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    def record_llm(self, usage: object | None) -> None:
        with self._lock:
            self.outer.add(usage)

    def record_final_turn(self, usage: object | None) -> None:
        with self._lock:
            self.final_turn.add(usage)

    def record_search_response(
        self, response: object, *, model: str | None = None
    ) -> None:
        """Record one hosted search response and the helper model that produced it."""

        with self._lock:
            self.search_calls += 1
            if model is not None:
                self.search_models.add(model)
            usage = _field(response, "usage")
            self.search.add(usage)
            self.hosted_tool_executions += _count_tool_executions(response)
            if usage is None:
                return
            connector_tokens = _as_int(_field(usage, "connector_tokens"))
            connectors = _field(usage, "connectors")
            if connector_tokens is not None or isinstance(connectors, Mapping):
                self.connector_metadata_responses += 1
            if connector_tokens is not None:
                self.connector_tokens_reported += connector_tokens
            if isinstance(connectors, Mapping):
                for name, value in connectors.items():
                    count = _as_int(value)
                    if count is None:
                        continue
                    key = str(name)
                    self.connectors_reported[key] = (
                        self.connectors_reported.get(key, 0) + count
                    )

    def record_search_failure(self, *, cancelled: bool = False) -> None:
        with self._lock:
            self.search_calls += 1
            if cancelled:
                self.search_cancellations += 1
            else:
                self.search_failures += 1
            self.search_unknown_billed_usage_calls += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            outer = self.outer.as_dict()
            search = self.search.as_dict()
            final_turn = self.final_turn.as_dict()
            outer_usd = estimate_model_tokens_usd(self.outer, OUTER_RATE_CARD)
            search_models = set(self.search_models)
            if self.search_calls == 0:
                search_status = "not_used"
            elif search_models and search_models != {HOSTED_SEARCH_MODEL}:
                search_status = "unknown"
            elif self.search.calls == 0:
                search_status = "no_reported_usage"
            elif search_models == {HOSTED_SEARCH_MODEL}:
                search_status = "known"
            else:
                search_status = "unknown"
            search_usd: float | None
            if search_status == "unknown":
                search_usd = None
            else:
                search_usd = estimate_model_tokens_usd(self.search, SEARCH_RATE_CARD)
            total_usd = (
                None if search_usd is None else round(outer_usd + search_usd, 6)
            )
            recorded_search_model = (
                next(iter(search_models)) if len(search_models) == 1 else None
            )
            if search_status == "unknown":
                search_pricing: dict[str, Any] = {
                    "status": search_status,
                    "model": recorded_search_model,
                    "models": sorted(search_models),
                    "reason": (
                        "No verified rate card for this search model; search "
                        "tokens are unpriced."
                    ),
                    "source": PRICING_SOURCE,
                    "as_of": PRICING_AS_OF,
                }
            else:
                search_pricing = {
                    **SEARCH_RATE_CARD.as_dict(),
                    "status": search_status,
                }
            bridge = {
                "calls": self.search_calls,
                "failures": self.search_failures,
                "cancellations": self.search_cancellations,
                "unknown_billed_usage_calls": self.search_unknown_billed_usage_calls,
                "hosted_tool_executions": self.hosted_tool_executions,
                "connector_tokens_reported": self.connector_tokens_reported,
                "connectors_reported": dict(self.connectors_reported),
                "connector_metadata_responses": self.connector_metadata_responses,
            }
        return {
            "outer": outer,
            "search": search,
            "final_turn_cross_check": final_turn,
            "search_bridge": bridge,
            "model_token_estimate_usd": {
                "outer": outer_usd,
                "search": search_usd,
                "total": total_usd,
                "search_pricing_status": search_status,
            },
            "pricing": {
                "source": PRICING_SOURCE,
                "as_of": PRICING_AS_OF,
                "outer": OUTER_RATE_CARD.as_dict(),
                "search": search_pricing,
            },
            "semantics": {
                "outer": (
                    "SDK TokenUsage; total_tokens is validated as input + output."
                ),
                "search": (
                    "Conversations usage for the Small 4 helper; total_tokens is "
                    "provider-reported and may include connector tokens, so it need "
                    "not equal prompt + completion."
                ),
                "final_turn_cross_check": (
                    "Not added to any total; the final model call is already in outer."
                ),
                "pricing": (
                    "Outer and search use separate verified rate cards. Search "
                    "cached-input pricing is not verified; reported search prompt "
                    "tokens are priced at the standard input rate. A custom search "
                    "model is flagged unknown and no total is claimed."
                ),
                "connectors": (
                    "Raw provider map, summed across responses; values are "
                    "provider-defined and may be call counts, not tokens."
                ),
                "connector_tokens": (
                    "Reported but unpriced; excluded from model_token_estimate_usd."
                ),
                "hosted_tool_executions": (
                    "Count of tool.execution output entries; a proxy for hosted "
                    "web_search executions, not a billed search count."
                ),
                "unknown_billed_usage_calls": (
                    "Bridge calls that failed or were cancelled after the client "
                    "call began; the request may or may not have reached the "
                    "provider, so any tokens are unobservable and excluded."
                ),
                "connector_metadata_responses": (
                    "Responses that reported connector metadata; 0 means unknown, "
                    "not zero connector usage."
                ),
                "calls_without_usage": (
                    "Calls whose usage was absent or had no recognizable token fields."
                ),
                "total_tokens_reported": (
                    "Sum of provider-reported totals. For outer, the SDK validates "
                    "total = input + output. For search, the generated Conversations "
                    "client defaults absent fields to 0 and the total may include "
                    "connector tokens."
                ),
            },
            "caveats": list(CAVEATS),
        }


class UsageHook(agents.Hook):
    """Fieldless public hook: the SDK reuses the authored instance.

    A Hook without fields runs on its one authored object, so the ``PrivateAttr``
    meter accumulates across every model call in the session. The meter is never
    serialized into session state.
    """

    _meter: UsageMeter = PrivateAttr()

    def __init__(self, meter: UsageMeter) -> None:
        super().__init__()
        self._meter = meter

    async def post_llm_call(
        self,
        ctx: agents.HookCallbackContext,
        invocation: agents.hook.PostLlmCall,
    ) -> None:
        self._meter.record_llm(invocation.usage)
        return None

    async def post_agent_turn(
        self,
        ctx: agents.HookCallbackContext,
        result: agents.hook.PostAgentTurn,
    ) -> None:
        self._meter.record_final_turn(result.usage)
        return None
