"""Module-level search tool and per-session runtime registry.

The SDK requires decorated tools to be defined at module scope. The tool
therefore resolves its backend, ledger, and inventory binding from a small
registry keyed by the current session. Local CLI runs register one runtime for
their session and remove it afterwards. A hosted process must register a
runtime bound to the sanitized inventory; without one, resolution fails closed
instead of sharing default evidence. The registry is in-memory only and is not
durable deployment state.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from .sdk_compat import prepare_sdk_environment

prepare_sdk_environment()

from mistralai import agents  # noqa: E402

from .evidence import (  # noqa: E402
    MAX_QUERY_CHARS,
    EvidenceLedger,
    LedgerRegistry,
    SearchBudgetExceeded,
    clean_text,
)
from .models import Inventory, InventoryComponent  # noqa: E402
from .search import SearchBackend, SearchBackendError  # noqa: E402

DEFAULT_MAX_SEARCH_CALLS = 4
MAX_SEARCH_CALLS_CEILING = 64
MAX_RESULTS_PER_CALL = 5


@dataclass
class ToolRuntime:
    backend: SearchBackend
    ledger: EvidenceLedger
    inventory: Inventory | None = None
    max_search_calls: int = DEFAULT_MAX_SEARCH_CALLS
    max_results_per_call: int = MAX_RESULTS_PER_CALL


def make_runtime(
    backend: SearchBackend,
    *,
    ledger: EvidenceLedger | None = None,
    inventory: Inventory | None = None,
    max_search_calls: int = DEFAULT_MAX_SEARCH_CALLS,
    max_results_per_call: int = MAX_RESULTS_PER_CALL,
) -> ToolRuntime:
    """Create a runtime with hard-clamped search limits."""

    return ToolRuntime(
        backend=backend,
        ledger=ledger if ledger is not None else EvidenceLedger(),
        inventory=inventory,
        max_search_calls=max(1, min(int(max_search_calls), MAX_SEARCH_CALLS_CEILING)),
        max_results_per_call=max(1, min(int(max_results_per_call), MAX_RESULTS_PER_CALL)),
    )


class RuntimeRegistry:
    """Resolve the search runtime for a tool call.

    Session-specific runtimes win. A default runtime is used only for an
    unregistered session and only when it carries a registered inventory;
    evidence is still per-turn. There is no session-less fallback, so a hosted
    process without an inventory binding fails closed instead of sharing
    another turn's evidence.
    """

    def __init__(self) -> None:
        self._by_session: dict[str, ToolRuntime] = {}
        self._default: ToolRuntime | None = None
        self._hosted_ledgers = LedgerRegistry()

    def register(self, session_id: str, runtime: ToolRuntime) -> None:
        self._by_session[session_id] = runtime

    def unregister(self, session_id: str) -> None:
        self._by_session.pop(session_id, None)

    def set_default(self, runtime: ToolRuntime) -> None:
        self._default = runtime

    def resolve(self, ctx: agents.ToolCallbackContext) -> ToolRuntime:
        session = getattr(ctx, "session", None)
        if session is None:
            raise RuntimeError(
                "no session context; refusing to share default search evidence"
            )
        registered = self._by_session.get(session.id)
        if registered is not None:
            if registered.inventory is None:
                raise RuntimeError(
                    "registered search runtime has no inventory binding for this session"
                )
            return registered
        if self._default is not None and self._default.inventory is not None:
            return ToolRuntime(
                backend=self._default.backend,
                ledger=self._hosted_ledgers.for_turn(session.id, session.turn_id),
                inventory=self._default.inventory,
                max_search_calls=self._default.max_search_calls,
                max_results_per_call=self._default.max_results_per_call,
            )
        raise RuntimeError(
            "no search runtime with a registered inventory for this session"
        )


RUNTIME_REGISTRY = RuntimeRegistry()


def _safe_error(error: object) -> str:
    text = clean_text(error, 300)
    return text or type(error).__name__


def build_search_query(component: InventoryComponent, focus: str = "") -> str:
    """Build the search query from trusted inventory fields plus optional focus.

    The component name, version, and ecosystem prefix is always derived
    server-side, so a model-supplied focus string cannot replace it. The focus
    is appended only within the shared query bound.
    """

    prefix = clean_text(
        f"{component.name} {component.version} {component.ecosystem} "
        "security advisory",
        MAX_QUERY_CHARS,
    )
    focus_text = clean_text(focus, MAX_QUERY_CHARS)
    if not focus_text:
        return prefix
    remaining = MAX_QUERY_CHARS - len(prefix) - 1
    if remaining <= 0:
        return prefix
    return f"{prefix} {focus_text[:remaining]}"


def _result_payload(
    *,
    component_id: str,
    query: str,
    evidence: list[dict[str, str]],
    remaining_calls: int,
    error: str | None = None,
) -> str:
    payload: dict[str, Any] = {
        "component_id": component_id,
        "query": query,
        "evidence": evidence,
        "remaining_calls": remaining_calls,
    }
    if error is not None:
        payload["error"] = error
    return json.dumps(payload, ensure_ascii=False)


async def execute_search(
    runtime: ToolRuntime,
    component_id: str,
    query: str = "",
) -> str:
    """Run one bounded, component-attributed search and return a JSON result.

    The component ID is validated against the runtime's registered inventory
    before any budget reservation or backend call. The query prefix is derived
    from the trusted inventory fields; ``query`` is only an optional focus
    suffix. The shared ledger reserves one call before the backend await. This
    is the budget authority even when the hosted registry creates a new
    ``ToolRuntime`` for each tool call in the same turn. Cancellation settles
    the reservation as a failed attempt, so it still consumes budget.
    """

    component_text = clean_text(component_id, 64)
    if not component_text:
        return _result_payload(
            component_id="",
            query="",
            evidence=[],
            remaining_calls=runtime.ledger.remaining(runtime.max_search_calls),
            error="component_id is empty",
        )
    if runtime.inventory is None:
        return _result_payload(
            component_id=component_text,
            query="",
            evidence=[],
            remaining_calls=runtime.ledger.remaining(runtime.max_search_calls),
            error="no inventory is registered for this search runtime",
        )
    component = runtime.inventory.get_component(component_text)
    if component is None:
        return _result_payload(
            component_id=component_text,
            query="",
            evidence=[],
            remaining_calls=runtime.ledger.remaining(runtime.max_search_calls),
            error=f"unknown component_id: {component_text}",
        )

    query_text = build_search_query(component, query)
    try:
        reservation = runtime.ledger.reserve_search(
            query_text,
            runtime.max_search_calls,
            component_id=component.id,
        )
    except SearchBudgetExceeded:
        return _result_payload(
            component_id=component.id,
            query=query_text,
            evidence=[],
            remaining_calls=0,
            error=f"search budget exhausted (max {runtime.max_search_calls} calls per run)",
        )

    try:
        raw_results = await runtime.backend.search(query_text)
        bounded = list(raw_results)[: runtime.max_results_per_call]
    except asyncio.CancelledError:
        # The backend call may already be in flight. Consume the reservation so
        # a cancelled paid attempt cannot be retried without budget.
        runtime.ledger.cancel_reservation(reservation, "search call was cancelled")
        raise
    except SearchBackendError as error:
        runtime.ledger.fail_reservation(reservation, error)
        return _result_payload(
            component_id=component.id,
            query=query_text,
            evidence=[],
            remaining_calls=runtime.ledger.remaining(runtime.max_search_calls),
            error=_safe_error(error),
        )
    except Exception as error:  # noqa: BLE001 - tool boundary
        runtime.ledger.fail_reservation(reservation, error)
        return _result_payload(
            component_id=component.id,
            query=query_text,
            evidence=[],
            remaining_calls=runtime.ledger.remaining(runtime.max_search_calls),
            error=f"search backend failed: {_safe_error(error)}",
        )

    recorded = runtime.ledger.complete_reservation(reservation, bounded)
    remaining_after = runtime.ledger.remaining(runtime.max_search_calls)
    if not recorded:
        return _result_payload(
            component_id=component.id,
            query=query_text,
            evidence=[],
            remaining_calls=remaining_after,
            error="search returned no usable evidence URLs",
        )
    return _result_payload(
        component_id=component.id,
        query=query_text,
        evidence=[item.as_dict() for item in recorded],
        remaining_calls=remaining_after,
    )


@agents.tool(
    name="search_security_advisories",
    description=(
        "Search public security advisories for one inventory component. "
        "Pass component_id from the inventory; the tool builds the "
        "component/version query itself. Optionally add a short focus phrase. "
        "Returns evidence_id values and URLs; cite only these exact values."
    ),
    model_access="direct",
    annotations=agents.ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=True,
    ),
)
async def search_security_advisories(
    ctx: agents.ToolCallbackContext,
    /,
    component_id: str,
    query: str = "",
) -> str:
    """Search public security advisories for one inventory component."""

    runtime = RUNTIME_REGISTRY.resolve(ctx)
    return await execute_search(runtime, component_id, query)
