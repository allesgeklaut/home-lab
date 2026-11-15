from __future__ import annotations

import asyncio
import json

import pytest

from security_watch.evidence import EvidenceLedger, SearchBudgetExceeded, SearchResult
from security_watch.runtime import execute_search, make_runtime
from security_watch.search import FakeSearchBackend, SearchBackendError

from .helpers import URL1, URL2, inventory, results

WEB_PREFIX = "Acme Web App 2.4.1 npm security advisory"


def run(coro):
    return asyncio.run(coro)


def test_search_budget_is_hard_capped() -> None:
    backend = FakeSearchBackend(results())
    runtime = make_runtime(backend, inventory=inventory(), max_search_calls=2)
    first = json.loads(run(execute_search(runtime, "web-app", "one")))
    second = json.loads(run(execute_search(runtime, "web-app", "two")))
    third = json.loads(run(execute_search(runtime, "web-app", "three")))
    assert first["evidence"]
    assert second["evidence"]
    assert third["error"] == "search budget exhausted (max 2 calls per run)"
    assert len(backend.queries) == 2
    assert runtime.ledger.calls == 2
    assert runtime.ledger.successful_calls == 2


def test_search_results_are_capped_per_call() -> None:
    backend = FakeSearchBackend(results())
    runtime = make_runtime(
        backend,
        inventory=inventory(),
        max_search_calls=2,
        max_results_per_call=1,
    )
    payload = json.loads(run(execute_search(runtime, "web-app", "one")))
    assert len(payload["evidence"]) == 1
    assert payload["evidence"][0]["url"] == URL1
    assert len(runtime.ledger.evidence) == 1


def test_empty_search_is_recorded_and_reported() -> None:
    backend = FakeSearchBackend([])
    runtime = make_runtime(backend, inventory=inventory())
    payload = json.loads(run(execute_search(runtime, "web-app", "nothing")))
    assert payload["error"] == "search returned no usable evidence URLs"
    assert runtime.ledger.empty_calls == 1
    assert runtime.ledger.successful_calls == 0
    assert runtime.ledger.evidence == ()


def test_backend_failure_is_recorded_and_reported() -> None:
    backend = FakeSearchBackend(error=SearchBackendError("upstream unavailable"))
    runtime = make_runtime(backend, inventory=inventory())
    payload = json.loads(run(execute_search(runtime, "web-app", "failure")))
    assert "upstream unavailable" in payload["error"]
    assert runtime.ledger.failed_calls == 1
    assert runtime.ledger.successful_calls == 0
    assert runtime.ledger.evidence == ()


def test_unexpected_backend_exception_is_bounded() -> None:
    backend = FakeSearchBackend(error=RuntimeError("boom\nwith newline"))
    runtime = make_runtime(backend, inventory=inventory())
    payload = json.loads(run(execute_search(runtime, "web-app", "failure")))
    assert payload["error"] == "search backend failed: boom with newline"
    assert runtime.ledger.failed_calls == 1


def test_invalid_urls_are_ignored() -> None:
    backend = FakeSearchBackend(
        [
            SearchResult(title="bad scheme", url="file:///etc/passwd"),
            SearchResult(title="credentials", url="https://user:pass@example.test/x"),
            SearchResult(title="no host", url="https:///path"),
        ]
    )
    runtime = make_runtime(backend, inventory=inventory())
    payload = json.loads(run(execute_search(runtime, "web-app", "invalid")))
    assert payload["error"] == "search returned no usable evidence URLs"
    assert runtime.ledger.empty_calls == 1
    assert runtime.ledger.evidence == ()


def test_repeated_urls_reuse_evidence_id() -> None:
    backend = FakeSearchBackend(results())
    runtime = make_runtime(backend, inventory=inventory())
    first = json.loads(run(execute_search(runtime, "web-app", "one")))
    second = json.loads(run(execute_search(runtime, "web-app", "two")))
    assert [item["evidence_id"] for item in first["evidence"]] == ["E1", "E2"]
    assert [item["evidence_id"] for item in second["evidence"]] == ["E1", "E2"]
    assert len(runtime.ledger.evidence) == 2
    assert runtime.ledger.successful_calls == 2


def test_empty_component_id_does_not_consume_budget() -> None:
    backend = FakeSearchBackend(results())
    runtime = make_runtime(backend, inventory=inventory(), max_search_calls=1)
    payload = json.loads(run(execute_search(runtime, "   ")))
    assert payload["error"] == "component_id is empty"
    assert runtime.ledger.calls == 0
    assert backend.queries == []


def test_unknown_component_id_does_not_call_backend_or_use_budget() -> None:
    backend = FakeSearchBackend(results())
    runtime = make_runtime(backend, inventory=inventory(), max_search_calls=1)
    payload = json.loads(run(execute_search(runtime, "ghost-service")))
    assert "unknown component_id" in payload["error"]
    assert backend.queries == []
    assert runtime.ledger.calls == 0
    assert runtime.ledger.budget_used == 0


def test_runtime_without_inventory_fails_closed() -> None:
    backend = FakeSearchBackend(results())
    runtime = make_runtime(backend, max_search_calls=1)
    payload = json.loads(run(execute_search(runtime, "web-app")))
    assert "no inventory is registered" in payload["error"]
    assert backend.queries == []
    assert runtime.ledger.calls == 0


def test_query_is_bounded_and_keeps_component_prefix() -> None:
    backend = FakeSearchBackend(results())
    runtime = make_runtime(backend, inventory=inventory())
    run(execute_search(runtime, "web-app", "x" * 1000))
    query = backend.queries[0]
    assert len(query) == 400
    assert query.startswith(WEB_PREFIX)


def test_focus_cannot_remove_component_prefix() -> None:
    backend = FakeSearchBackend(results())
    runtime = make_runtime(backend, inventory=inventory())
    run(execute_search(runtime, "web-app", "unrelated vendor CVE"))
    query = backend.queries[0]
    assert query.startswith(WEB_PREFIX)
    assert "unrelated vendor CVE" in query


def test_search_attempt_records_component_attribution() -> None:
    backend = FakeSearchBackend(results())
    runtime = make_runtime(backend, inventory=inventory())
    payload = json.loads(run(execute_search(runtime, "api-service", "advisory")))
    assert payload["component_id"] == "api-service"
    snapshot = runtime.ledger.snapshot()
    assert snapshot["attempts"][0]["component_id"] == "api-service"
    assert snapshot["searched_component_ids"] == ["api-service"]
    assert snapshot["attempted_component_ids"] == ["api-service"]


def test_runtime_clamps_limits() -> None:
    runtime = make_runtime(
        FakeSearchBackend(),
        max_search_calls=999,
        max_results_per_call=99,
    )
    assert runtime.max_search_calls == 64
    assert runtime.max_results_per_call == 5


def test_duplicate_urls_within_one_call_are_deduped() -> None:
    backend = FakeSearchBackend([results()[0], results()[0], results()[1]])
    runtime = make_runtime(backend, inventory=inventory())
    payload = json.loads(run(execute_search(runtime, "web-app", "dupes")))
    assert [item["url"] for item in payload["evidence"]] == [URL1, URL2]


def test_concurrent_searches_cannot_exceed_budget() -> None:
    backend = FakeSearchBackend(results(), delay=0.05)
    runtime = make_runtime(backend, inventory=inventory(), max_search_calls=2)

    async def scenario() -> list[str]:
        return await asyncio.gather(
            *(execute_search(runtime, "web-app", f"query-{index}") for index in range(6))
        )

    payloads = [json.loads(item) for item in run(scenario())]
    assert len(backend.queries) == 2
    assert len(set(backend.queries)) == 2
    assert runtime.ledger.calls == 2
    assert runtime.ledger.successful_calls == 2
    assert runtime.ledger.in_flight_calls == 0
    errors = [payload["error"] for payload in payloads if "error" in payload]
    assert len(errors) == 4
    assert all("budget exhausted" in error for error in errors)


def test_reservation_counts_in_flight_calls_before_completion() -> None:
    ledger = EvidenceLedger()
    reservation = ledger.reserve_search("first", max_calls=1, component_id="web-app")
    assert ledger.calls == 0
    assert ledger.in_flight_calls == 1
    assert ledger.budget_used == 1
    assert ledger.remaining(1) == 0
    with pytest.raises(SearchBudgetExceeded):
        ledger.reserve_search("second", max_calls=1, component_id="web-app")

    recorded = ledger.complete_reservation(reservation, results())
    assert [item.evidence_id for item in recorded] == ["E1", "E2"]
    assert ledger.calls == 1
    assert ledger.in_flight_calls == 0
    assert ledger.budget_used == 1
    assert ledger.successful_calls == 1
    assert ledger.successful_component_ids == {"web-app"}


def test_reservation_cannot_be_settled_twice() -> None:
    ledger = EvidenceLedger()
    reservation = ledger.reserve_search("first", max_calls=1, component_id="web-app")
    ledger.complete_reservation(reservation, results())
    with pytest.raises(RuntimeError, match="already settled"):
        ledger.fail_reservation(reservation, "late failure")
    assert ledger.calls == 1
    assert ledger.failed_calls == 0


class ExplodingResult:
    title = "boom"
    snippet = ""

    @property
    def url(self) -> str:
        raise RuntimeError("boom")


def test_recording_failure_consumes_reservation() -> None:
    ledger = EvidenceLedger()
    reservation = ledger.reserve_search("query", max_calls=1, component_id="web-app")
    with pytest.raises(RuntimeError, match="boom"):
        ledger.complete_reservation(reservation, [ExplodingResult()])
    assert ledger.in_flight_calls == 0
    assert ledger.calls == 1
    assert ledger.failed_calls == 1
    assert ledger.successful_calls == 0
    assert ledger.remaining(1) == 0
    assert ledger.attempts[0].component_id == "web-app"


def test_cancelled_search_consumes_budget() -> None:
    backend = FakeSearchBackend(results(), delay=5.0)
    runtime = make_runtime(backend, inventory=inventory(), max_search_calls=1)

    async def scenario() -> None:
        task = asyncio.create_task(execute_search(runtime, "web-app", "slow"))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    assert len(backend.queries) == 1
    assert runtime.ledger.calls == 1
    assert runtime.ledger.failed_calls == 1
    assert runtime.ledger.in_flight_calls == 0
    assert runtime.ledger.attempts[0].outcome == "error"
    assert runtime.ledger.attempts[0].component_id == "web-app"
    assert "cancelled" in (runtime.ledger.attempts[0].error or "").lower()

    retry = json.loads(run(execute_search(runtime, "web-app", "retry")))
    assert "budget exhausted" in retry["error"]
    assert len(backend.queries) == 1
