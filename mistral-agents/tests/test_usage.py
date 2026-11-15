from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from mistralai.client.types import UNSET

from security_watch.agent_factory import build_agent
from security_watch.runner import run_security_watch
from security_watch.runtime import make_runtime
from security_watch.search import FakeSearchBackend, HOSTED_SEARCH_MODEL
from security_watch.usage import (
    ModelUsage,
    OUTER_RATE_CARD,
    PRICING_AS_OF,
    PRICING_SOURCE,
    UsageHook,
    UsageMeter,
    estimate_model_tokens_usd,
)

from .helpers import inventory, report_payload, reply, results, search_call


def run(coro):
    return asyncio.run(coro)


def test_missing_usage_is_counted_not_zero() -> None:
    usage = ModelUsage()
    usage.add(None)
    assert usage.calls == 1
    assert usage.calls_without_usage == 1
    assert usage.input_tokens == 0
    assert usage.output_tokens == 0


def test_unrecognized_usage_shape_is_counted_as_missing() -> None:
    usage = ModelUsage()
    usage.add(SimpleNamespace(total_tokens=10))
    assert usage.calls == 1
    assert usage.calls_without_usage == 1


def test_prompt_completion_aliases_are_read() -> None:
    usage = ModelUsage()
    usage.add(SimpleNamespace(prompt_tokens=12, completion_tokens=3, total_tokens=15))
    assert usage.input_tokens == 12
    assert usage.output_tokens == 3
    assert usage.total_tokens_reported == 15


def test_subsets_are_not_double_counted() -> None:
    usage = ModelUsage()
    usage.add(
        SimpleNamespace(
            input_tokens=1_000_000,
            cached_input_tokens=400_000,
            cache_write_tokens=10_000,
            output_tokens=100_000,
            reasoning_tokens=50_000,
            total_tokens=1_100_000,
        )
    )
    assert estimate_model_tokens_usd(usage, OUTER_RATE_CARD) == pytest.approx(
        0.6 * 0.70 + 0.4 * 0.07 + 0.1 * 2.10
    )


def test_surfaces_are_additive_but_separate() -> None:
    meter = UsageMeter()
    meter.record_llm(
        SimpleNamespace(
            input_tokens=1_000_000,
            cached_input_tokens=0,
            output_tokens=0,
            total_tokens=1_000_000,
        )
    )
    meter.record_search_response(
        SimpleNamespace(
            usage=SimpleNamespace(
                prompt_tokens=1_000_000,
                completion_tokens=0,
                total_tokens=1_000_000,
                connector_tokens=UNSET,
                connectors=UNSET,
            ),
            outputs=[],
        ),
        model=HOSTED_SEARCH_MODEL,
    )
    snapshot = meter.snapshot()
    assert snapshot["outer"]["input_tokens"] == 1_000_000
    assert snapshot["search"]["input_tokens"] == 1_000_000
    assert snapshot["model_token_estimate_usd"]["outer"] == pytest.approx(0.70)
    assert snapshot["model_token_estimate_usd"]["search"] == pytest.approx(0.15)
    assert snapshot["model_token_estimate_usd"]["total"] == pytest.approx(0.85)
    assert snapshot["pricing"]["outer"]["model"] == OUTER_RATE_CARD.model
    assert snapshot["pricing"]["search"]["model"] == HOSTED_SEARCH_MODEL


def test_final_turn_is_cross_check_only() -> None:
    meter = UsageMeter()
    usage = SimpleNamespace(
        input_tokens=1_000_000,
        output_tokens=0,
        total_tokens=1_000_000,
    )
    meter.record_llm(usage)
    meter.record_final_turn(usage)
    snapshot = meter.snapshot()
    assert snapshot["outer"]["calls"] == 1
    assert snapshot["final_turn_cross_check"]["calls"] == 1
    assert snapshot["model_token_estimate_usd"]["total"] == pytest.approx(0.70)


def test_search_failure_is_unknown_billed_usage() -> None:
    meter = UsageMeter()
    meter.record_search_failure()
    snapshot = meter.snapshot()
    assert snapshot["search_bridge"]["calls"] == 1
    assert snapshot["search_bridge"]["failures"] == 1
    assert snapshot["search_bridge"]["unknown_billed_usage_calls"] == 1
    assert snapshot["search"]["calls"] == 0


def test_search_cancellation_is_unknown_billed_usage() -> None:
    meter = UsageMeter()
    meter.record_search_failure(cancelled=True)
    snapshot = meter.snapshot()
    assert snapshot["search_bridge"]["cancellations"] == 1
    assert snapshot["search_bridge"]["unknown_billed_usage_calls"] == 1


def test_bridge_call_count_matches_responses_plus_failures() -> None:
    meter = UsageMeter()
    meter.record_search_response(SimpleNamespace(usage=None, outputs=[]))
    meter.record_search_failure()
    meter.record_search_failure(cancelled=True)
    snapshot = meter.snapshot()
    assert snapshot["search_bridge"]["calls"] == 3
    assert snapshot["search"]["calls"] == 1
    assert snapshot["search"]["calls_without_usage"] == 1
    assert snapshot["search_bridge"]["failures"] == 1
    assert snapshot["search_bridge"]["cancellations"] == 1


def test_connector_metadata_is_raw_and_not_priced() -> None:
    meter = UsageMeter()
    meter.record_search_response(
        SimpleNamespace(
            usage=SimpleNamespace(
                prompt_tokens=47_701,
                completion_tokens=19_253,
                total_tokens=111_729,
                connector_tokens=44_775,
                connectors={"web_search": 8},
            ),
            outputs=[
                SimpleNamespace(type="tool.execution"),
                SimpleNamespace(type="message.output"),
            ],
        ),
        model=HOSTED_SEARCH_MODEL,
    )
    snapshot = meter.snapshot()
    search = snapshot["search"]
    bridge = snapshot["search_bridge"]
    assert search["input_tokens"] == 47_701
    assert search["output_tokens"] == 19_253
    assert search["total_tokens_reported"] == 111_729
    assert search["total_tokens_reported"] != (
        search["input_tokens"] + search["output_tokens"]
    )
    assert bridge["connector_tokens_reported"] == 44_775
    assert bridge["connectors_reported"] == {"web_search": 8}
    assert bridge["hosted_tool_executions"] == 1
    assert bridge["connector_metadata_responses"] == 1
    assert snapshot["model_token_estimate_usd"]["search"] == pytest.approx(
        (47_701 * 0.15 + 19_253 * 0.60) / 1_000_000,
        abs=1e-6,
    )


def test_unset_connector_metadata_is_not_a_zero_claim() -> None:
    meter = UsageMeter()
    meter.record_search_response(
        SimpleNamespace(
            usage=SimpleNamespace(
                prompt_tokens=10,
                completion_tokens=2,
                total_tokens=12,
                connector_tokens=UNSET,
                connectors=UNSET,
            ),
            outputs=[],
        )
    )
    snapshot = meter.snapshot()
    assert snapshot["search_bridge"]["connector_metadata_responses"] == 0
    assert snapshot["search_bridge"]["connector_tokens_reported"] == 0
    assert snapshot["search_bridge"]["connectors_reported"] == {}


def test_pricing_block_names_both_models_and_rates() -> None:
    meter = UsageMeter()
    meter.record_search_response(
        SimpleNamespace(
            usage=SimpleNamespace(
                prompt_tokens=1,
                completion_tokens=1,
                total_tokens=2,
            ),
            outputs=[],
        ),
        model=HOSTED_SEARCH_MODEL,
    )
    snapshot = meter.snapshot()
    pricing = snapshot["pricing"]
    assert pricing["source"] == PRICING_SOURCE
    assert pricing["as_of"] == PRICING_AS_OF
    assert pricing["outer"] == {
        "model": "mistral-large-4",
        "input_per_million_usd": 0.70,
        "cached_input_per_million_usd": 0.07,
        "output_per_million_usd": 2.10,
        "cached_input_pricing": "verified published cached-input rate",
        "source": "https://docs.mistral.ai/models/mistral-large-4-0.md",
        "as_of": PRICING_AS_OF,
    }
    assert pricing["search"]["model"] == HOSTED_SEARCH_MODEL
    assert pricing["search"]["input_per_million_usd"] == 0.15
    assert pricing["search"]["cached_input_per_million_usd"] is None
    assert pricing["search"]["output_per_million_usd"] == 0.60
    assert pricing["search"]["source"] == PRICING_SOURCE
    assert pricing["search"]["as_of"] == PRICING_AS_OF
    assert pricing["search"]["status"] == "known"


def test_unknown_search_model_does_not_apply_small4_rates() -> None:
    meter = UsageMeter()
    meter.record_search_response(
        SimpleNamespace(
            usage=SimpleNamespace(
                prompt_tokens=1_000_000,
                completion_tokens=0,
                total_tokens=1_000_000,
            ),
            outputs=[],
        ),
        model="custom-search-model",
    )
    snapshot = meter.snapshot()
    assert snapshot["model_token_estimate_usd"]["search"] is None
    assert snapshot["model_token_estimate_usd"]["total"] is None
    assert snapshot["model_token_estimate_usd"]["search_pricing_status"] == "unknown"
    assert snapshot["pricing"]["search"]["status"] == "unknown"
    assert snapshot["pricing"]["search"]["model"] == "custom-search-model"
    assert "input_per_million_usd" not in snapshot["pricing"]["search"]


def test_search_response_without_model_is_unknown() -> None:
    meter = UsageMeter()
    meter.record_search_response(
        SimpleNamespace(
            usage=SimpleNamespace(
                prompt_tokens=1,
                completion_tokens=1,
                total_tokens=2,
            ),
            outputs=[],
        )
    )
    snapshot = meter.snapshot()
    assert snapshot["model_token_estimate_usd"]["search"] is None
    assert snapshot["pricing"]["search"]["status"] == "unknown"


def test_snapshot_is_json_serializable() -> None:
    meter = UsageMeter()
    meter.record_llm(
        SimpleNamespace(
            input_tokens=10,
            cached_input_tokens=2,
            output_tokens=3,
            reasoning_tokens=1,
            total_tokens=13,
        )
    )
    meter.record_search_response(
        SimpleNamespace(
            usage=SimpleNamespace(
                prompt_tokens=5,
                completion_tokens=1,
                total_tokens=9,
                connector_tokens=3,
                connectors={"web_search": 1},
            ),
            outputs=[SimpleNamespace(type="tool.execution")],
        )
    )
    json.dumps(meter.snapshot())


def test_usage_hook_accumulates_across_scripted_model_calls(tmp_path, no_network) -> None:
    from mistralai import agents

    meter = UsageMeter()
    agent = build_agent(hooks=[UsageHook(meter)]).override(
        model=agents.testing.ScriptedModel(
            [
                search_call(),
                search_call(),
                reply(report_payload()),
            ]
        )
    )
    runtime = make_runtime(FakeSearchBackend(results()), inventory=inventory())
    result = run(
        run_security_watch(
            agent,
            inventory(),
            runtime=runtime,
            timeout=10,
            state_dir=tmp_path,
            usage=meter,
        )
    )
    assert result.status == "completed"
    assert meter.outer.calls == 3
    assert meter.outer.calls_without_usage == 0
    assert meter.final_turn.calls == 1
    assert result.usage["outer"]["calls"] == 3
    state = json.loads((tmp_path / "runs" / result.run_id / "run.json").read_text())
    assert state["usage"]["outer"]["calls"] == 3
    assert state["usage"]["model_token_estimate_usd"]["total"] == 0.0
