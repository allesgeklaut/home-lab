from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from security_watch.agent_factory import build_agent
from security_watch.evidence import EvidenceLedger, SearchResult
from security_watch.models import Inventory, SecurityReport
from security_watch.runtime import ToolRuntime, make_runtime
from security_watch.search import FakeSearchBackend

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "inventory.json"

INVENTORY = {
    "schema_version": 1,
    "environment": "test",
    "components": [
        {
            "id": "web-app",
            "name": "Acme Web App",
            "version": "2.4.1",
            "ecosystem": "npm",
        },
        {
            "id": "api-service",
            "name": "Acme API",
            "version": "1.8.0",
            "ecosystem": "PyPI",
        },
        {
            "id": "edge-proxy",
            "name": "Acme Edge Proxy",
            "version": "0.9.3",
            "ecosystem": "container",
        },
    ],
}

URL1 = "https://advisories.example.test/web-app-1"
URL2 = "https://advisories.example.test/api-2"


def inventory() -> Inventory:
    return Inventory.model_validate(INVENTORY)


def results() -> list[SearchResult]:
    return [
        SearchResult(title="Web App advisory", url=URL1, snippet="Synthetic web advisory."),
        SearchResult(title="API advisory", url=URL2, snippet="Synthetic API advisory."),
    ]


def ledger_with_evidence() -> EvidenceLedger:
    ledger = EvidenceLedger()
    ledger.record_success("web app advisory", results(), component_id="web-app")
    return ledger


def ledger_with_full_coverage() -> EvidenceLedger:
    """Successful evidence-bearing searches for every inventory component."""

    ledger = EvidenceLedger()
    for component_id, query in (
        ("web-app", "web app advisory"),
        ("api-service", "api advisory"),
        ("edge-proxy", "edge proxy advisory"),
    ):
        ledger.record_success(query, results(), component_id=component_id)
    return ledger


def report_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "assessment": "actionable_findings",
        "summary": "One possible issue needs deployment review.",
        "findings": [
            {
                "component_id": "web-app",
                "title": "Possible issue in Acme Web App",
                "severity": "high",
                "status": "possible",
                "evidence_ids": ["E1"],
                "sources": [URL1],
                "impact": "Synthetic impact.",
                "recommendation": "Verify the feature and upgrade.",
                "uncertainty": "Inventory-only evidence.",
            }
        ],
        "checked_component_ids": ["web-app"],
        "evidence_ids": ["E1", "E2"],
        "sources": [URL1, URL2],
        "limitations": ["Synthetic test evidence."],
        "research_notes": "Searched the synthetic backend.",
    }
    payload.update(overrides)
    return payload


def report(**overrides: Any) -> SecurityReport:
    return SecurityReport.model_validate(report_payload(**overrides))


def runtime_with_evidence(**kwargs: Any) -> ToolRuntime:
    """A runtime whose ledger is populated by the agent's own search call."""

    return make_runtime(FakeSearchBackend(results()), inventory=inventory(), **kwargs)


def scripted_agent(steps: list[Any], *, max_iterations: int = 6, hooks=None):
    from mistralai import agents

    return build_agent(max_iterations=max_iterations, hooks=hooks).override(
        model=agents.testing.ScriptedModel(steps)
    )


def search_call(component_id: str = "web-app", query: str = ""):
    from mistralai import agents

    arguments: dict[str, Any] = {"component_id": component_id}
    if query:
        arguments["query"] = query
    return agents.testing.call("search_security_advisories", **arguments)


def reply(payload: dict[str, Any] | str):
    from mistralai import agents

    text = payload if isinstance(payload, str) else json.dumps(payload)
    return agents.testing.reply(text)
