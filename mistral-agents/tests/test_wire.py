from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx2
import pytest
from pydantic import ValidationError

from mistralai import agents

from security_watch.agent_factory import MODEL, build_agent
from security_watch.models import Finding, SecurityReport
from security_watch.validation import validate_report
from security_watch.wire import WireSecurityReport

from .helpers import inventory, ledger_with_evidence, report_payload

# The provider rejected the full generated schema. The exact single keyword is
# not isolated; this is the annotation/constraint group the schema-aware live
# probe accepted.
_UNSUPPORTED_KEYWORDS = frozenset(
    {
        "title",
        "description",
        "default",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "pattern",
        "format",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "uniqueItems",
    }
)
_SCHEMA_MAP_KEYWORDS = frozenset(
    {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
)
_SCHEMA_KEYWORDS = frozenset(
    {"additionalProperties", "contains", "propertyNames", "not", "if", "then", "else"}
)
_SCHEMA_LIST_KEYWORDS = frozenset({"anyOf", "oneOf", "allOf", "prefixItems"})
_ALLOWED_KEYWORDS = frozenset(
    {"$defs", "$ref", "type", "properties", "required", "additionalProperties", "items", "enum"}
)


def _walk_schema(node: Any, path: str = "") -> Any:
    """Yield (path, keyword) for schema keywords, not property/definition names."""

    if not isinstance(node, dict):
        return
    for key, value in node.items():
        yield f"{path}/{key}", key
        if key in _SCHEMA_MAP_KEYWORDS and isinstance(value, dict):
            for name, subschema in value.items():
                yield from _walk_schema(subschema, f"{path}/{key}/{name}")
        elif key == "items":
            if isinstance(value, list):
                for index, item in enumerate(value):
                    yield from _walk_schema(item, f"{path}/items/{index}")
            else:
                yield from _walk_schema(value, f"{path}/items")
        elif key in _SCHEMA_KEYWORDS:
            yield from _walk_schema(value, f"{path}/{key}")
        elif key in _SCHEMA_LIST_KEYWORDS and isinstance(value, list):
            for index, item in enumerate(value):
                yield from _walk_schema(item, f"{path}/{key}/{index}")


def _assert_provider_compatible(schema: dict[str, Any]) -> None:
    keywords = {keyword for _, keyword in _walk_schema(schema)}
    assert keywords <= _ALLOWED_KEYWORDS
    assert not (keywords & _UNSUPPORTED_KEYWORDS)


def test_wire_schema_strips_unsupported_keywords() -> None:
    _assert_provider_compatible(WireSecurityReport.model_json_schema())


def test_full_schema_still_contains_unsupported_keywords() -> None:
    keywords = {keyword for _, keyword in _walk_schema(SecurityReport.model_json_schema())}
    assert keywords & _UNSUPPORTED_KEYWORDS


def test_wire_schema_preserves_report_shape() -> None:
    schema = WireSecurityReport.model_json_schema()
    finding = schema["$defs"]["Finding"]

    assert set(schema["properties"]) == set(SecurityReport.model_fields)
    assert set(finding["properties"]) == set(Finding.model_fields)
    assert set(schema["required"]) == {
        "assessment",
        "summary",
        "limitations",
        "research_notes",
    }
    assert set(finding["required"]) == set(Finding.model_fields)

    # "title" is a property name here, not a stripped annotation keyword.
    assert finding["properties"]["title"]["type"] == "string"
    assert "title" in finding["properties"]
    assert "title" in finding["required"]
    assert "title" not in finding
    assert "description" not in finding

    assert schema["properties"]["findings"]["items"] == {"$ref": "#/$defs/Finding"}
    assert finding["properties"]["component_id"]["type"] == "string"
    assert finding["properties"]["evidence_ids"]["type"] == "array"
    assert finding["properties"]["sources"]["type"] == "array"
    assert finding["properties"]["severity"]["enum"] == [
        "critical",
        "high",
        "medium",
        "low",
        "info",
    ]
    assert finding["properties"]["status"]["enum"] == [
        "confirmed",
        "possible",
        "unverified",
    ]
    assert schema["properties"]["assessment"]["enum"] == [
        "actionable_findings",
        "no_actionable_findings",
        "inconclusive",
    ]
    assert schema["additionalProperties"] is False
    assert finding["additionalProperties"] is False


def test_wire_model_is_a_security_report() -> None:
    report = WireSecurityReport.model_validate(report_payload())
    assert isinstance(report, WireSecurityReport)
    assert isinstance(report, SecurityReport)
    assert report.findings[0].title == "Possible issue in Acme Web App"


def test_wire_model_passes_local_ledger_validation() -> None:
    report = WireSecurityReport.model_validate(report_payload())
    validated = validate_report(report, inventory(), ledger_with_evidence())
    assert isinstance(validated, WireSecurityReport)


def _bad_component_id(payload: dict[str, Any]) -> None:
    payload["findings"][0]["component_id"] = "UPPER"


def _bad_evidence_id(payload: dict[str, Any]) -> None:
    payload["findings"][0]["evidence_ids"] = ["bad"]


def _bad_checked_component_id(payload: dict[str, Any]) -> None:
    payload["checked_component_ids"] = ["UPPER"]


def _bad_url(payload: dict[str, Any]) -> None:
    payload["sources"] = ["not-a-url"]


def _empty_limitations(payload: dict[str, Any]) -> None:
    payload["limitations"] = []


def _oversized_summary(payload: dict[str, Any]) -> None:
    payload["summary"] = "x" * 2001


def _oversized_finding_title(payload: dict[str, Any]) -> None:
    payload["findings"][0]["title"] = "x" * 201


def _oversized_limitation(payload: dict[str, Any]) -> None:
    payload["limitations"] = ["x" * 501]


@pytest.mark.parametrize(
    ("mutate", "error_type", "location"),
    [
        (_bad_component_id, "string_pattern_mismatch", ("findings", 0, "component_id")),
        (_bad_evidence_id, "string_pattern_mismatch", ("findings", 0, "evidence_ids", 0)),
        (
            _bad_checked_component_id,
            "string_pattern_mismatch",
            ("checked_component_ids", 0),
        ),
        (_bad_url, "url_parsing", ("sources", 0)),
        (_empty_limitations, "too_short", ("limitations",)),
        (_oversized_summary, "string_too_long", ("summary",)),
        (_oversized_finding_title, "string_too_long", ("findings", 0, "title")),
        (_oversized_limitation, "string_too_long", ("limitations", 0)),
    ],
    ids=[
        "bad-component-id",
        "bad-evidence-id",
        "bad-checked-component-id",
        "bad-url",
        "empty-limitations",
        "oversized-summary",
        "oversized-finding-title",
        "oversized-limitation",
    ],
)
def test_wire_model_keeps_strict_local_validation(
    mutate: Any, error_type: str, location: tuple[Any, ...]
) -> None:
    payload = report_payload()
    mutate(payload)
    with pytest.raises(ValidationError) as excinfo:
        WireSecurityReport.model_validate(payload)
    assert any(
        error["type"] == error_type and error["loc"] == location
        for error in excinfo.value.errors()
    )


class _FakeStreamResponse:
    status_code = 200
    headers = {"content-type": "text/event-stream"}
    text = ""

    def __init__(self, request: Any, body: bytes) -> None:
        self.request = request
        self.url = str(request.url)
        self._body = body

    async def aiter_bytes(self) -> Any:
        yield self._body

    async def aclose(self) -> None:
        return None


def _sse_body(payload: dict[str, Any]) -> bytes:
    chunk = {
        "id": "cmpl-offline",
        "model": MODEL,
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": json.dumps(payload)},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    return f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()


def test_agent_serializes_relaxed_wire_schema(monkeypatch, no_network) -> None:
    monkeypatch.delenv("MISTRAL_CLIENT_API_KEY", raising=False)
    monkeypatch.setenv("MISTRAL_API_KEY", "offline-test-key")
    for name in ("MISTRAL_API_URL", "MISTRAL_SERVER_URL", "SERVER_URL"):
        monkeypatch.delenv(name, raising=False)

    requests: list[dict[str, Any]] = []

    async def fake_send(self: Any, request: Any, *, stream: bool = False, **kwargs: Any) -> Any:
        assert "chat/completions" in str(request.url)
        requests.append(json.loads(request.content))
        return _FakeStreamResponse(request, _sse_body(report_payload()))

    monkeypatch.setattr(httpx2.AsyncClient, "send", fake_send)

    async def run() -> Any:
        agent = build_agent()
        session = await agent.start_session()
        try:
            return await session.run("Return the structured report.")
        finally:
            await session.cancel()

    state = asyncio.run(run())

    assert isinstance(state, agents.Completed)
    assert isinstance(state.output, WireSecurityReport)
    assert isinstance(state.output, SecurityReport)
    assert len(requests) == 1

    body = requests[0]
    assert body["model"] == MODEL
    assert body["reasoning_effort"] == "medium"
    assert body["stream"] is True
    assert [tool["function"]["name"] for tool in body["tools"]] == [
        "search_security_advisories"
    ]

    response_format = body["response_format"]
    assert response_format["type"] == "json_schema"
    json_schema = response_format["json_schema"]
    assert json_schema["name"] == "WireSecurityReport"
    assert json_schema["strict"] is True
    schema = json_schema["schema"]

    _assert_provider_compatible(schema)
    assert set(schema["properties"]) == set(SecurityReport.model_fields)
    assert set(schema["required"]) == set(SecurityReport.model_fields)

    finding = schema["$defs"]["Finding"]
    assert set(finding["properties"]) == set(Finding.model_fields)
    assert set(finding["required"]) == set(Finding.model_fields)
    assert finding["properties"]["title"]["type"] == "string"
    assert "title" in finding["required"]
    assert "title" not in finding
    assert schema["properties"]["findings"]["items"] == {"$ref": "#/$defs/Finding"}
    assert finding["properties"]["severity"]["enum"] == [
        "critical",
        "high",
        "medium",
        "low",
        "info",
    ]
    assert finding["properties"]["status"]["enum"] == [
        "confirmed",
        "possible",
        "unverified",
    ]
    assert schema["properties"]["assessment"]["enum"] == [
        "actionable_findings",
        "no_actionable_findings",
        "inconclusive",
    ]
    assert schema["additionalProperties"] is False
    assert finding["additionalProperties"] is False
