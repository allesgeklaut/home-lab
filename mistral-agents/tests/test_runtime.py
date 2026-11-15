from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from security_watch.runtime import RuntimeRegistry, execute_search, make_runtime
from security_watch.search import FakeSearchBackend

from .helpers import inventory, results


def test_hosted_default_gets_a_fresh_ledger_per_turn() -> None:
    registry = RuntimeRegistry()
    registry.set_default(
        make_runtime(FakeSearchBackend(results()), inventory=inventory())
    )
    first = registry.resolve(
        SimpleNamespace(session=SimpleNamespace(id="session-1", turn_id="turn-1"))
    )
    second = registry.resolve(
        SimpleNamespace(session=SimpleNamespace(id="session-1", turn_id="turn-2"))
    )
    assert first.ledger is not second.ledger
    assert first.backend is second.backend
    assert first.inventory is second.inventory


def test_session_runtime_wins_over_default() -> None:
    registry = RuntimeRegistry()
    registry.set_default(
        make_runtime(FakeSearchBackend(results()), inventory=inventory())
    )
    session_runtime = make_runtime(FakeSearchBackend([]), inventory=inventory())
    registry.register("session-1", session_runtime)
    resolved = registry.resolve(
        SimpleNamespace(session=SimpleNamespace(id="session-1", turn_id="turn-1"))
    )
    assert resolved is session_runtime


def test_resolve_without_session_fails_closed() -> None:
    registry = RuntimeRegistry()
    registry.set_default(
        make_runtime(FakeSearchBackend(results()), inventory=inventory())
    )
    with pytest.raises(RuntimeError, match="no session context"):
        registry.resolve(SimpleNamespace(session=None))


def test_resolve_without_registered_inventory_fails_closed() -> None:
    registry = RuntimeRegistry()
    registry.set_default(make_runtime(FakeSearchBackend(results())))
    with pytest.raises(RuntimeError, match="registered inventory"):
        registry.resolve(
            SimpleNamespace(session=SimpleNamespace(id="session-1", turn_id="turn-1"))
        )


def test_registered_runtime_without_inventory_fails_closed() -> None:
    registry = RuntimeRegistry()
    registry.register("session-1", make_runtime(FakeSearchBackend(results())))
    with pytest.raises(RuntimeError, match="no inventory binding"):
        registry.resolve(
            SimpleNamespace(session=SimpleNamespace(id="session-1", turn_id="turn-1"))
        )


def test_unregistered_session_without_default_fails_closed() -> None:
    registry = RuntimeRegistry()
    with pytest.raises(RuntimeError, match="registered inventory"):
        registry.resolve(
            SimpleNamespace(session=SimpleNamespace(id="session-1", turn_id="turn-1"))
        )


def test_same_hosted_turn_resolves_separate_runtimes_sharing_one_ledger() -> None:
    registry = RuntimeRegistry()
    backend = FakeSearchBackend(results(), delay=0.05)
    registry.set_default(
        make_runtime(
            backend,
            inventory=inventory(),
            max_search_calls=1,
        )
    )
    ctx = SimpleNamespace(session=SimpleNamespace(id="session-1", turn_id="turn-1"))

    first = registry.resolve(ctx)
    second = registry.resolve(ctx)
    assert first is not second
    assert first.ledger is second.ledger

    async def scenario() -> list[str]:
        return await asyncio.gather(
            execute_search(first, "web-app", "first query"),
            execute_search(second, "web-app", "second query"),
        )

    payloads = [json.loads(item) for item in asyncio.run(scenario())]
    assert len(backend.queries) == 1
    assert first.ledger.calls == 1
    assert first.ledger.successful_calls == 1
    assert first.ledger.in_flight_calls == 0
    errors = [payload["error"] for payload in payloads if "error" in payload]
    assert len(errors) == 1
    assert "budget exhausted" in errors[0]
