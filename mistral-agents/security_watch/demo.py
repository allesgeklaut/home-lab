"""Deterministic offline demo data.

The demo uses the real SDK loop with ``agents.testing.ScriptedModel`` and a
fake search backend. It needs no API key and performs no network access.
"""

from __future__ import annotations

import json

from .sdk_compat import prepare_sdk_environment

prepare_sdk_environment()

from mistralai import agents  # noqa: E402

from .evidence import SearchResult  # noqa: E402
from .search import FakeSearchBackend  # noqa: E402

DEMO_RESULTS = [
    SearchResult(
        title="Acme Web App 2.4.1 path traversal advisory",
        url="https://advisories.example.test/acme-web-app-2026-001",
        snippet=(
            "Synthetic advisory: Acme Web App before 2.4.3 may allow path traversal "
            "when an untrusted archive is imported. Applicability to this deployment "
            "is not established by the inventory alone."
        ),
    ),
    SearchResult(
        title="Acme API 1.8.0 dependency advisory",
        url="https://advisories.example.test/acme-api-2026-002",
        snippet=(
            "Synthetic advisory: Acme API 1.8.0 pins a parsing library with a "
            "denial-of-service issue. Confirm the deployed build and code path."
        ),
    ),
]

DEMO_REPORT = {
    "assessment": "actionable_findings",
    "summary": (
        "Two synthetic advisories match inventory components at the version level. "
        "Neither is confirmed as exploitable in this deployment."
    ),
    "findings": [
        {
            "component_id": "web-app",
            "title": "Possible path traversal in Acme Web App 2.4.1",
            "severity": "high",
            "status": "possible",
            "evidence_ids": ["E1"],
            "sources": ["https://advisories.example.test/acme-web-app-2026-001"],
            "impact": (
                "An attacker who can supply an archive could potentially read or "
                "write outside the intended import directory."
            ),
            "recommendation": (
                "Verify whether the archive-import feature is enabled, then upgrade "
                "to a fixed release or disable the feature."
            ),
            "uncertainty": (
                "Synthetic advisory and inventory-only evidence; the vulnerable code "
                "path and deployment exposure are unverified."
            ),
        },
        {
            "component_id": "api-service",
            "title": "Possible denial of service in Acme API 1.8.0 dependency",
            "severity": "medium",
            "status": "possible",
            "evidence_ids": ["E2"],
            "sources": ["https://advisories.example.test/acme-api-2026-002"],
            "impact": (
                "A crafted request could consume parser resources and degrade "
                "availability if the affected dependency is reachable."
            ),
            "recommendation": (
                "Confirm the dependency version in the built artifact and apply the "
                "vendor's fixed release."
            ),
            "uncertainty": (
                "The inventory records the application version, not the resolved "
                "transitive dependency or its runtime use."
            ),
        },
    ],
    "checked_component_ids": ["web-app", "api-service"],
    "evidence_ids": ["E1", "E2"],
    "sources": [
        "https://advisories.example.test/acme-web-app-2026-001",
        "https://advisories.example.test/acme-api-2026-002",
    ],
    "limitations": [
        "Synthetic fixture data; no live advisory database was queried.",
        "Only two of three inventory components were searched; coverage is partial.",
        "Inventory versions do not prove that a vulnerable feature or code path is used.",
        "This is advisory research, not a vulnerability scan or an all-clear.",
    ],
    "research_notes": (
        "Ran two synthetic searches with the injected demo backend: one for the web "
        "application and one for the API service. The edge proxy was not searched, so "
        "this report does not claim coverage for it."
    ),
}

DEMO_WEB_QUERY = "Acme Web App 2.4.1 npm security advisory path traversal"
DEMO_API_QUERY = "Acme API 1.8.0 PyPI security advisory dependency denial of service"


def demo_backend() -> FakeSearchBackend:
    return FakeSearchBackend(
        results_by_query={
            DEMO_WEB_QUERY: [DEMO_RESULTS[0]],
            DEMO_API_QUERY: [DEMO_RESULTS[1]],
        }
    )


def demo_scripted_model() -> agents.testing.ScriptedModel:
    return agents.testing.ScriptedModel(
        [
            agents.testing.call(
                "search_security_advisories",
                component_id="web-app",
                query="path traversal",
            ),
            agents.testing.call(
                "search_security_advisories",
                component_id="api-service",
                query="dependency denial of service",
            ),
            agents.testing.reply(json.dumps(DEMO_REPORT)),
        ]
    )
