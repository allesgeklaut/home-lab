from __future__ import annotations

import asyncio
import json

import pytest

from security_watch.evidence import SearchResult
from security_watch.models import Inventory
from security_watch.runner import _build_payload, run_security_watch
from security_watch.runtime import make_runtime
from security_watch.search import FakeSearchBackend, SearchBackendError

from .helpers import (
    URL1,
    URL2,
    inventory,
    report_payload,
    reply,
    results,
    runtime_with_evidence,
    scripted_agent,
    search_call,
)


def run(coro):
    return asyncio.run(coro)


class SelectiveBackend:
    """Fake backend that fails or returns empty results for matching queries."""

    def __init__(
        self,
        *,
        fail: set[str] | None = None,
        empty: set[str] | None = None,
    ) -> None:
        self.fail = fail or set()
        self.empty = empty or set()
        self.queries: list[str] = []

    async def search(self, query: str) -> list[SearchResult]:
        self.queries.append(query)
        if any(marker in query for marker in self.fail):
            raise SearchBackendError("upstream unavailable")
        if any(marker in query for marker in self.empty):
            return []
        return results()


def all_clear_payload() -> dict:
    return report_payload(
        assessment="no_actionable_findings",
        findings=[],
        checked_component_ids=["web-app", "api-service", "edge-proxy"],
        evidence_ids=["E1", "E2"],
        sources=[URL1, URL2],
    )


def zero_evidence_inconclusive_payload() -> dict:
    return report_payload(
        assessment="inconclusive",
        findings=[],
        checked_component_ids=[],
        evidence_ids=[],
        sources=[],
        limitations=["No usable search evidence was returned."],
    )


def test_completed_run_validates_and_writes_state(tmp_path, no_network) -> None:
    runtime = runtime_with_evidence()
    agent = scripted_agent([search_call(), reply(report_payload())])
    result = run(
        run_security_watch(
            agent,
            inventory(),
            runtime=runtime,
            timeout=10,
            max_iterations=6,
            state_dir=tmp_path,
        )
    )
    assert result.status == "completed"
    assert result.report is not None
    assert result.report.assessment == "actionable_findings"
    assert result.ledger.successful_calls == 1
    assert result.stop_reason is None
    run_dir = tmp_path / "runs" / result.run_id
    assert (run_dir / "run.json").exists()
    assert not (run_dir / "report.json").exists()
    assert not (run_dir / "ledger.json").exists()
    state = json.loads((run_dir / "run.json").read_text())
    assert state["status"] == "completed"
    assert state["report"]["assessment"] == "actionable_findings"
    assert state["ledger"]["successful_calls"] == 1
    assert state["ledger"]["attempts"][0]["component_id"] == "web-app"
    assert state["validation"] == "local ledger-backed validation passed"
    assert (run_dir.stat().st_mode & 0o777) == 0o700
    assert ((run_dir / "run.json").stat().st_mode & 0o777) == 0o600


def test_runner_binds_inventory_to_unbound_runtime(tmp_path, no_network) -> None:
    runtime = make_runtime(FakeSearchBackend(results()))
    assert runtime.inventory is None
    result = run(
        run_security_watch(
            scripted_agent([search_call(), reply(report_payload())]),
            inventory(),
            runtime=runtime,
            timeout=10,
            state_dir=tmp_path,
        )
    )
    assert result.status == "completed"
    assert runtime.inventory == inventory()


def test_runner_rejects_mismatched_runtime_inventory(tmp_path, no_network) -> None:
    other_inventory = inventory().model_copy(update={"environment": "other"})
    runtime = make_runtime(
        FakeSearchBackend(results()),
        inventory=other_inventory,
    )
    with pytest.raises(ValueError, match="does not match"):
        run(
            run_security_watch(
                scripted_agent([search_call(), reply(report_payload())]),
                inventory(),
                runtime=runtime,
                timeout=10,
                state_dir=tmp_path,
            )
        )


def test_no_search_rejected(tmp_path, no_network) -> None:
    runtime = make_runtime(FakeSearchBackend(results()), inventory=inventory())
    agent = scripted_agent([reply(report_payload())])
    result = run(
        run_security_watch(
            agent,
            inventory(),
            runtime=runtime,
            timeout=10,
            state_dir=tmp_path,
        )
    )
    assert result.status == "rejected"
    assert "no search tool/backend attempt" in (result.error or "")
    assert result.ledger.calls == 0
    state = json.loads((tmp_path / "runs" / result.run_id / "run.json").read_text())
    assert state["report"] is None


def test_iteration_limit_is_rejected(tmp_path, no_network) -> None:
    runtime = runtime_with_evidence()
    agent = scripted_agent(
        [search_call(), reply(report_payload())],
        max_iterations=1,
    )
    result = run(
        run_security_watch(
            agent,
            inventory(),
            runtime=runtime,
            timeout=10,
            max_iterations=1,
            state_dir=tmp_path,
        )
    )
    assert result.status == "rejected"
    assert result.stop_reason == "iteration_limit"
    assert "stopped early" in (result.error or "")


def test_wall_clock_timeout_cancels(tmp_path, no_network) -> None:
    runtime = make_runtime(
        FakeSearchBackend(results(), delay=5.0),
        inventory=inventory(),
    )
    agent = scripted_agent([search_call(), reply(report_payload())])
    result = run(
        run_security_watch(
            agent,
            inventory(),
            runtime=runtime,
            timeout=0.2,
            state_dir=tmp_path,
        )
    )
    assert result.status == "timeout"
    assert "wall-clock timeout" in (result.error or "")
    assert result.duration_seconds < 3.0
    assert result.report is None


def test_invalid_output_is_rejected(tmp_path, no_network) -> None:
    runtime = runtime_with_evidence()
    agent = scripted_agent([reply("this is not JSON")])
    result = run(
        run_security_watch(
            agent,
            inventory(),
            runtime=runtime,
            timeout=10,
            state_dir=tmp_path,
        )
    )
    assert result.status == "rejected"
    assert "did not match the report schema" in (result.error or "")


def test_empty_search_results_are_rejected(tmp_path, no_network) -> None:
    runtime = make_runtime(FakeSearchBackend([]), inventory=inventory())
    agent = scripted_agent([search_call(), reply(report_payload())])
    result = run(
        run_security_watch(
            agent,
            inventory(),
            runtime=runtime,
            timeout=10,
            state_dir=tmp_path,
        )
    )
    assert result.status == "rejected"
    assert result.ledger.empty_calls == 1
    assert "no successful search evidence" in (result.error or "")


def test_search_backend_failure_is_rejected(tmp_path, no_network) -> None:
    runtime = make_runtime(
        FakeSearchBackend(error=SearchBackendError("offline")),
        inventory=inventory(),
    )
    agent = scripted_agent([search_call(), reply(report_payload())])
    result = run(
        run_security_watch(
            agent,
            inventory(),
            runtime=runtime,
            timeout=10,
            state_dir=tmp_path,
        )
    )
    assert result.status == "rejected"
    assert result.ledger.failed_calls == 1
    assert "no successful search evidence" in (result.error or "")


def test_evidence_does_not_carry_between_runs(tmp_path, no_network) -> None:
    first_runtime = runtime_with_evidence()
    first = run(
        run_security_watch(
            scripted_agent([search_call(), reply(report_payload())]),
            inventory(),
            runtime=first_runtime,
            timeout=10,
            state_dir=tmp_path,
        )
    )
    assert first.status == "completed"

    second_runtime = make_runtime(FakeSearchBackend([]), inventory=inventory())
    second = run(
        run_security_watch(
            scripted_agent([reply(report_payload())]),
            inventory(),
            runtime=second_runtime,
            timeout=10,
            state_dir=tmp_path,
        )
    )
    assert second.status == "rejected"
    assert second.ledger.evidence == ()
    assert first.run_id != second.run_id


def test_all_clear_requires_reported_full_coverage(tmp_path, no_network) -> None:
    runtime = runtime_with_evidence()
    payload = report_payload(
        assessment="no_actionable_findings",
        findings=[],
        checked_component_ids=["web-app"],
        evidence_ids=["E1"],
        sources=[URL1],
    )
    result = run(
        run_security_watch(
            scripted_agent(
                [
                    search_call("web-app"),
                    search_call("api-service"),
                    search_call("edge-proxy"),
                    reply(payload),
                ]
            ),
            inventory(),
            runtime=runtime,
            timeout=10,
            state_dir=tmp_path,
        )
    )
    assert result.status == "rejected"
    assert "every inventory component" in (result.error or "")


def test_all_clear_requires_searched_full_coverage(tmp_path, no_network) -> None:
    runtime = runtime_with_evidence()
    result = run(
        run_security_watch(
            scripted_agent([search_call("web-app"), reply(all_clear_payload())]),
            inventory(),
            runtime=runtime,
            timeout=10,
            state_dir=tmp_path,
        )
    )
    assert result.status == "rejected"
    assert "successful evidence-bearing search for every inventory component" in (
        result.error or ""
    )


def test_all_clear_with_full_coverage_is_accepted(tmp_path, no_network) -> None:
    runtime = runtime_with_evidence()
    payload = report_payload(
        assessment="no_actionable_findings",
        summary="No actionable evidence was found for the synthetic inventory.",
        findings=[],
        checked_component_ids=["web-app", "api-service", "edge-proxy"],
        evidence_ids=["E1", "E2"],
        sources=[URL1, URL2],
        limitations=["Synthetic evidence only; this is not a vulnerability scan."],
    )
    result = run(
        run_security_watch(
            scripted_agent(
                [
                    search_call("web-app"),
                    search_call("api-service"),
                    search_call("edge-proxy"),
                    reply(payload),
                ]
            ),
            inventory(),
            runtime=runtime,
            timeout=10,
            state_dir=tmp_path,
        )
    )
    assert result.status == "completed"
    assert result.report is not None
    assert result.report.assessment == "no_actionable_findings"
    assert result.report.findings == []
    assert result.ledger.successful_component_ids == {
        "web-app",
        "api-service",
        "edge-proxy",
    }


def test_all_clear_rejected_after_failed_search(tmp_path, no_network) -> None:
    runtime = make_runtime(
        SelectiveBackend(fail={"Acme Web App"}),
        inventory=inventory(),
        max_search_calls=4,
    )
    result = run(
        run_security_watch(
            scripted_agent(
                [
                    search_call("web-app"),
                    search_call("api-service"),
                    search_call("edge-proxy"),
                    reply(all_clear_payload()),
                ]
            ),
            inventory(),
            runtime=runtime,
            timeout=10,
            state_dir=tmp_path,
        )
    )
    assert result.status == "rejected"
    assert result.ledger.failed_calls == 1
    assert result.ledger.successful_calls == 2
    assert "failed search attempts" in (result.error or "")


def test_all_clear_rejected_after_empty_search(tmp_path, no_network) -> None:
    runtime = make_runtime(
        SelectiveBackend(empty={"Acme Web App"}),
        inventory=inventory(),
        max_search_calls=4,
    )
    result = run(
        run_security_watch(
            scripted_agent(
                [
                    search_call("web-app"),
                    search_call("api-service"),
                    search_call("edge-proxy"),
                    reply(all_clear_payload()),
                ]
            ),
            inventory(),
            runtime=runtime,
            timeout=10,
            state_dir=tmp_path,
        )
    )
    assert result.status == "rejected"
    assert result.ledger.empty_calls == 1
    assert result.ledger.successful_calls == 2
    assert "empty search results" in (result.error or "")


@pytest.mark.parametrize("mode", ["fail", "empty"])
def test_actionable_partial_coverage_with_unavailable_search_is_accepted(
    tmp_path, no_network, mode
) -> None:
    runtime = make_runtime(
        SelectiveBackend(**{mode: {"Acme API"}}),
        inventory=inventory(),
        max_search_calls=4,
    )
    payload = report_payload(
        assessment="actionable_findings",
        checked_component_ids=["web-app"],
        evidence_ids=["E1"],
        sources=[URL1],
        limitations=["API service search was unavailable; coverage is partial."],
    )
    result = run(
        run_security_watch(
            scripted_agent(
                [
                    search_call("web-app"),
                    search_call("api-service"),
                    reply(payload),
                ]
            ),
            inventory(),
            runtime=runtime,
            timeout=10,
            state_dir=tmp_path,
        )
    )
    assert result.status == "completed"
    assert result.report is not None
    assert result.report.assessment == "actionable_findings"
    assert result.report.checked_component_ids == ["web-app"]
    assert result.ledger.successful_calls == 1
    assert result.ledger.failed_calls == (1 if mode == "fail" else 0)
    assert result.ledger.empty_calls == (1 if mode == "empty" else 0)
    assert "coverage is partial" in result.report.limitations[0]


def test_overbudget_inventory_cannot_claim_all_clear(tmp_path, no_network) -> None:
    five_components = Inventory.model_validate(
        {
            "schema_version": 1,
            "environment": "test",
            "components": [
                {
                    "id": f"component-{index}",
                    "name": f"Component {index}",
                    "version": "1.0.0",
                    "ecosystem": "npm",
                }
                for index in range(1, 6)
            ],
        }
    )
    runtime = make_runtime(
        FakeSearchBackend(results()),
        inventory=five_components,
        max_search_calls=4,
    )
    payload = report_payload(
        assessment="no_actionable_findings",
        findings=[],
        checked_component_ids=[f"component-{index}" for index in range(1, 6)],
        evidence_ids=["E1", "E2"],
        sources=[URL1, URL2],
    )
    result = run(
        run_security_watch(
            scripted_agent(
                [
                    search_call(f"component-{index}")
                    for index in range(1, 5)
                ]
                + [reply(payload)]
            ),
            five_components,
            runtime=runtime,
            timeout=10,
            state_dir=tmp_path,
        )
    )
    assert result.status == "rejected"
    assert result.ledger.successful_calls == 4
    assert "successful evidence-bearing search for every inventory component" in (
        result.error or ""
    )


def test_overbudget_inventory_can_report_inconclusive(tmp_path, no_network) -> None:
    five_components = Inventory.model_validate(
        {
            "schema_version": 1,
            "environment": "test",
            "components": [
                {
                    "id": f"component-{index}",
                    "name": f"Component {index}",
                    "version": "1.0.0",
                    "ecosystem": "npm",
                }
                for index in range(1, 6)
            ],
        }
    )
    runtime = make_runtime(
        FakeSearchBackend(results()),
        inventory=five_components,
        max_search_calls=4,
    )
    payload = report_payload(
        assessment="inconclusive",
        findings=[],
        checked_component_ids=[f"component-{index}" for index in range(1, 5)],
        evidence_ids=["E1", "E2"],
        sources=[URL1, URL2],
        limitations=["The fifth component was not searched within the budget."],
    )
    result = run(
        run_security_watch(
            scripted_agent(
                [
                    search_call(f"component-{index}")
                    for index in range(1, 5)
                ]
                + [reply(payload)]
            ),
            five_components,
            runtime=runtime,
            timeout=10,
            state_dir=tmp_path,
        )
    )
    assert result.status == "completed"
    assert result.report is not None
    assert result.report.assessment == "inconclusive"
    assert result.ledger.successful_component_ids == {
        "component-1",
        "component-2",
        "component-3",
        "component-4",
    }


def test_inconclusive_without_findings_is_accepted(tmp_path, no_network) -> None:
    runtime = runtime_with_evidence()
    payload = report_payload(
        assessment="inconclusive",
        findings=[],
        checked_component_ids=["web-app"],
        evidence_ids=["E1"],
        sources=[URL1],
        limitations=["Only one component was checked."],
    )
    result = run(
        run_security_watch(
            scripted_agent([search_call(), reply(payload)]),
            inventory(),
            runtime=runtime,
            timeout=10,
            state_dir=tmp_path,
        )
    )
    assert result.status == "completed"
    assert result.report is not None
    assert result.report.assessment == "inconclusive"


def test_zero_evidence_inconclusive_after_failed_search(tmp_path, no_network) -> None:
    runtime = make_runtime(
        FakeSearchBackend(error=SearchBackendError("offline")),
        inventory=inventory(),
    )
    result = run(
        run_security_watch(
            scripted_agent(
                [search_call("web-app"), reply(zero_evidence_inconclusive_payload())]
            ),
            inventory(),
            runtime=runtime,
            timeout=10,
            state_dir=tmp_path,
        )
    )
    assert result.status == "completed"
    assert result.report is not None
    assert result.report.assessment == "inconclusive"
    assert result.report.checked_component_ids == []
    assert result.report.evidence_ids == []
    assert result.ledger.failed_calls == 1


def test_zero_evidence_inconclusive_after_empty_search(tmp_path, no_network) -> None:
    runtime = make_runtime(FakeSearchBackend([]), inventory=inventory())
    result = run(
        run_security_watch(
            scripted_agent(
                [search_call("web-app"), reply(zero_evidence_inconclusive_payload())]
            ),
            inventory(),
            runtime=runtime,
            timeout=10,
            state_dir=tmp_path,
        )
    )
    assert result.status == "completed"
    assert result.report is not None
    assert result.report.assessment == "inconclusive"
    assert result.ledger.empty_calls == 1


def test_zero_evidence_inconclusive_without_search_is_rejected(
    tmp_path, no_network
) -> None:
    runtime = runtime_with_evidence()
    result = run(
        run_security_watch(
            scripted_agent([reply(zero_evidence_inconclusive_payload())]),
            inventory(),
            runtime=runtime,
            timeout=10,
            state_dir=tmp_path,
        )
    )
    assert result.status == "rejected"
    assert "no search tool/backend attempt" in (result.error or "")
    assert result.ledger.calls == 0


def test_ledger_snapshot_is_embedded_in_run_json(tmp_path, no_network) -> None:
    runtime = runtime_with_evidence()
    result = run(
        run_security_watch(
            scripted_agent([search_call(), reply(report_payload())]),
            inventory(),
            runtime=runtime,
            timeout=10,
            state_dir=tmp_path,
        )
    )
    state = json.loads((tmp_path / "runs" / result.run_id / "run.json").read_text())
    ledger = state["ledger"]
    assert ledger["successful_calls"] == 1
    assert ledger["in_flight_calls"] == 0
    assert ledger["budget_used"] == 1
    assert ledger["searched_component_ids"] == ["web-app"]
    assert ledger["evidence"][0]["evidence_id"] == "E1"
    assert ledger["evidence"][0]["url"] == URL1


def test_research_payload_is_lean() -> None:
    payload = json.loads(_build_payload(inventory(), runtime_with_evidence()))
    assert set(payload) == {
        "as_of_utc",
        "task",
        "inventory",
        "max_search_calls",
        "max_results_per_call",
    }
    assert "rules" not in payload
    assert "response_schema" not in payload


def test_completed_run_writes_usage_and_effective_limits(tmp_path, no_network) -> None:
    from security_watch.usage import UsageHook, UsageMeter

    meter = UsageMeter()
    runtime = runtime_with_evidence()
    agent = scripted_agent(
        [search_call(), reply(report_payload())],
        max_iterations=80,
        hooks=[UsageHook(meter)],
    )
    result = run(
        run_security_watch(
            agent,
            inventory(),
            runtime=runtime,
            timeout=10,
            max_iterations=999,
            state_dir=tmp_path,
            usage=meter,
        )
    )
    assert result.status == "completed"
    assert result.limits["max_iterations"] == 80
    assert result.usage["outer"]["calls"] == 2
    state = json.loads((tmp_path / "runs" / result.run_id / "run.json").read_text())
    assert state["usage"]["outer"]["calls"] == 2
    assert state["usage"]["model_token_estimate_usd"]["total"] == 0.0


def test_rejected_run_writes_usage(tmp_path, no_network) -> None:
    from security_watch.usage import UsageHook, UsageMeter

    meter = UsageMeter()
    runtime = make_runtime(FakeSearchBackend(results()), inventory=inventory())
    result = run(
        run_security_watch(
            scripted_agent(
                [reply(report_payload())], hooks=[UsageHook(meter)]
            ),
            inventory(),
            runtime=runtime,
            timeout=10,
            state_dir=tmp_path,
            usage=meter,
        )
    )
    assert result.status == "rejected"
    assert result.usage["outer"]["calls"] == 1
    state = json.loads((tmp_path / "runs" / result.run_id / "run.json").read_text())
    assert state["usage"]["outer"]["calls"] == 1


def test_timeout_run_writes_usage(tmp_path, no_network) -> None:
    from security_watch.usage import UsageHook, UsageMeter

    meter = UsageMeter()
    runtime = make_runtime(
        FakeSearchBackend(results(), delay=5.0),
        inventory=inventory(),
    )
    result = run(
        run_security_watch(
            scripted_agent(
                [search_call(), reply(report_payload())],
                hooks=[UsageHook(meter)],
            ),
            inventory(),
            runtime=runtime,
            timeout=0.2,
            state_dir=tmp_path,
            usage=meter,
        )
    )
    assert result.status == "timeout"
    assert result.usage["outer"]["calls"] >= 1
    state = json.loads((tmp_path / "runs" / result.run_id / "run.json").read_text())
    assert "usage" in state
