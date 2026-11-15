from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from security_watch.search import (
    HOSTED_SEARCH_MAX_OUTPUT_TOKENS,
    HOSTED_SEARCH_MODEL,
    HostedSearchBridge,
    SearchBackendError,
    extract_hosted_search_evidence,
)
from security_watch.usage import UsageMeter


class FakeConversations:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    async def start_async(self, **kwargs):
        self.kwargs = kwargs
        return self.response


class FakeClient:
    def __init__(self, response):
        self.conversations = FakeConversations(response)
        self.beta = SimpleNamespace(conversations=self.conversations)


def hosted_response() -> SimpleNamespace:
    return SimpleNamespace(
        outputs=[
            SimpleNamespace(
                type="tool.execution",
                info={
                    "result": json.dumps(
                        {
                            "results": [
                                {
                                    "title": "Tool result",
                                    "url": "https://tool.example.test/a",
                                    "snippet": "From tool metadata.",
                                }
                            ]
                        }
                    )
                },
            ),
            SimpleNamespace(
                type="message.output",
                content=[
                    SimpleNamespace(
                        type="text",
                        text="The model says https://model-invented.example.test/x",
                    ),
                    SimpleNamespace(
                        type="tool_reference",
                        title="Reference result",
                        url="https://reference.example.test/b",
                        description="From tool_reference metadata.",
                    ),
                ],
            ),
        ]
    )


def test_extracts_only_tool_metadata_urls() -> None:
    results = extract_hosted_search_evidence(hosted_response())
    assert [item.url for item in results] == [
        "https://tool.example.test/a",
        "https://reference.example.test/b",
    ]
    assert all("model-invented" not in item.url for item in results)


def test_text_only_response_has_no_evidence() -> None:
    response = SimpleNamespace(
        outputs=[
            SimpleNamespace(
                type="message.output",
                content=[
                    SimpleNamespace(
                        type="text",
                        text="Only model text with https://invented.example.test/x",
                    )
                ],
            )
        ]
    )
    assert extract_hosted_search_evidence(response) == []


def test_hosted_search_model_is_small4() -> None:
    assert HOSTED_SEARCH_MODEL == "mistral-small-2603"


def test_bridge_bounds_calls_and_disables_retries() -> None:
    client = FakeClient(hosted_response())
    bridge = HostedSearchBridge(client=client)
    results = asyncio.run(bridge.search("Acme Web App 2.4.1 advisory"))
    assert [item.url for item in results] == [
        "https://tool.example.test/a",
        "https://reference.example.test/b",
    ]
    kwargs = client.conversations.kwargs
    assert kwargs["model"] == HOSTED_SEARCH_MODEL
    assert kwargs["inputs"] == "Acme Web App 2.4.1 advisory"
    assert kwargs["tools"] == [{"type": "web_search"}]
    assert kwargs["store"] is False
    assert kwargs["completion_args"]["max_tokens"] == HOSTED_SEARCH_MAX_OUTPUT_TOKENS
    assert kwargs["completion_args"]["reasoning_effort"] == "none"
    assert kwargs["retries"].strategy == "none"
    assert kwargs["retries"].retry_connection_errors is False
    assert kwargs["timeout_ms"] == 60_000
    assert "server_url" not in kwargs


def test_bridge_requires_key_without_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    bridge = HostedSearchBridge()
    with pytest.raises(SearchBackendError, match="MISTRAL_API_KEY is not set"):
        asyncio.run(bridge.search("query"))


def test_bridge_empty_query_rejected_without_client() -> None:
    bridge = HostedSearchBridge(client=object())
    with pytest.raises(SearchBackendError, match="search query is empty"):
        asyncio.run(bridge.search("   "))


def test_bridge_rejects_unbounded_configuration() -> None:
    with pytest.raises(ValueError):
        HostedSearchBridge(max_output_tokens=4097)
    with pytest.raises(ValueError):
        HostedSearchBridge(timeout_seconds=0)
    with pytest.raises(ValueError):
        HostedSearchBridge(max_results=11)


def test_bridge_records_usage_and_tool_executions() -> None:
    response = hosted_response()
    response.usage = SimpleNamespace(
        prompt_tokens=47_701,
        completion_tokens=19_253,
        total_tokens=111_729,
        connector_tokens=44_775,
        connectors={"web_search": 8},
    )
    meter = UsageMeter()
    bridge = HostedSearchBridge(client=FakeClient(response), meter=meter)
    asyncio.run(bridge.search("Acme Web App 2.4.1 advisory"))
    snapshot = meter.snapshot()
    assert snapshot["search"]["calls"] == 1
    assert snapshot["search"]["input_tokens"] == 47_701
    assert snapshot["search"]["output_tokens"] == 19_253
    assert snapshot["search"]["total_tokens_reported"] == 111_729
    assert snapshot["search_bridge"]["connector_tokens_reported"] == 44_775
    assert snapshot["search_bridge"]["connectors_reported"] == {"web_search": 8}
    assert snapshot["search_bridge"]["hosted_tool_executions"] == 1
    assert snapshot["pricing"]["search"]["model"] == HOSTED_SEARCH_MODEL
    assert snapshot["model_token_estimate_usd"]["search"] == pytest.approx(
        (47_701 * 0.15 + 19_253 * 0.60) / 1_000_000,
        abs=1e-6,
    )


def test_bridge_custom_model_flags_search_pricing_unknown() -> None:
    response = hosted_response()
    response.usage = SimpleNamespace(
        prompt_tokens=1_000_000,
        completion_tokens=0,
        total_tokens=1_000_000,
    )
    meter = UsageMeter()
    bridge = HostedSearchBridge(
        client=FakeClient(response),
        model="custom-search-model",
        meter=meter,
    )
    asyncio.run(bridge.search("Acme Web App 2.4.1 advisory"))
    snapshot = meter.snapshot()
    assert snapshot["model_token_estimate_usd"]["search"] is None
    assert snapshot["model_token_estimate_usd"]["total"] is None
    assert snapshot["pricing"]["search"]["status"] == "unknown"
    assert snapshot["pricing"]["search"]["model"] == "custom-search-model"


def test_bridge_records_missing_usage_as_missing() -> None:
    response = hosted_response()
    response.usage = None
    meter = UsageMeter()
    bridge = HostedSearchBridge(client=FakeClient(response), meter=meter)
    asyncio.run(bridge.search("Acme Web App 2.4.1 advisory"))
    snapshot = meter.snapshot()
    assert snapshot["search"]["calls"] == 1
    assert snapshot["search"]["calls_without_usage"] == 1


class FailingConversations:
    async def start_async(self, **kwargs):
        raise RuntimeError("upstream unavailable")


class CancellingConversations:
    async def start_async(self, **kwargs):
        raise asyncio.CancelledError()


def test_bridge_failure_is_unknown_billed_usage() -> None:
    meter = UsageMeter()
    client = SimpleNamespace(
        beta=SimpleNamespace(conversations=FailingConversations())
    )
    bridge = HostedSearchBridge(client=client, meter=meter)
    with pytest.raises(SearchBackendError, match="hosted search failed"):
        asyncio.run(bridge.search("query"))
    snapshot = meter.snapshot()
    assert snapshot["search_bridge"]["failures"] == 1
    assert snapshot["search_bridge"]["unknown_billed_usage_calls"] == 1
    assert snapshot["search"]["calls"] == 0


def test_bridge_cancellation_is_unknown_billed_usage() -> None:
    meter = UsageMeter()
    client = SimpleNamespace(
        beta=SimpleNamespace(conversations=CancellingConversations())
    )
    bridge = HostedSearchBridge(client=client, meter=meter)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bridge.search("query"))
    snapshot = meter.snapshot()
    assert snapshot["search_bridge"]["cancellations"] == 1
    assert snapshot["search_bridge"]["unknown_billed_usage_calls"] == 1
