"""Append-only evidence ledger.

The model never gets to define provenance. Every evidence ID and URL in a
report must resolve to a result that this ledger recorded from an actual
search backend call in the same run/turn. Search attempts also record the
validated component attribution used for coverage checks.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Literal, Sequence
from urllib.parse import urlsplit

from pydantic import HttpUrl, ValidationError

MAX_URL_CHARS = 2048
MAX_TITLE_CHARS = 200
MAX_SNIPPET_CHARS = 600
MAX_QUERY_CHARS = 400
MAX_ERROR_CHARS = 300


@dataclass(frozen=True)
class SearchResult:
    """One result returned by a search backend."""

    title: str
    url: str
    snippet: str = ""


@dataclass(frozen=True)
class EvidenceItem:
    evidence_id: str
    query: str
    title: str
    url: str
    snippet: str

    def as_dict(self) -> dict[str, str]:
        return {
            "evidence_id": self.evidence_id,
            "query": self.query,
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
        }


@dataclass
class SearchAttempt:
    query: str
    outcome: Literal["evidence", "empty", "error"]
    component_id: str | None = None
    evidence_ids: list[str] = field(default_factory=list)
    error: str | None = None
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def as_dict(self) -> dict[str, object]:
        return {
            "query": self.query,
            "outcome": self.outcome,
            "component_id": self.component_id,
            "evidence_ids": list(self.evidence_ids),
            "error": self.error,
            "at": self.at,
        }


def clean_text(value: object, limit: int) -> str:
    """Collapse whitespace and bound a string for prompts, state, and errors."""

    if value is None:
        return ""
    text = " ".join(str(value).replace("\x00", " ").split())
    return text[:limit]


def redact_text(value: object, limit: int) -> str:
    """Bound text and remove the live API key if it appears."""

    text = clean_text(value, limit)
    key = os.environ.get("MISTRAL_API_KEY")
    if key:
        text = text.replace(key, "[REDACTED]")
    return text


def normalize_url(raw: object) -> str | None:
    """Return the canonical Pydantic ``HttpUrl`` string, or None.

    The ledger and the report schema use the same ``HttpUrl`` normalization, so
    IDNA hosts, percent-encoded paths, dot segments, and default ports compare
    identically. Security guards run before and after canonicalization:
    http(s) only, no credentials, non-empty host, valid port, and bounded
    length. Invalid values are rejected rather than repaired.
    """

    if not isinstance(raw, str):
        return None
    candidate = raw.strip()
    if not candidate or len(candidate) > MAX_URL_CHARS:
        return None

    # Pre-canonicalization guards. Keep these independent of HttpUrl so a
    # malformed value such as ``https:///path`` cannot be reinterpreted as a
    # host, and credentials are never normalized into an acceptable URL.
    try:
        parts = urlsplit(candidate)
        if parts.scheme.lower() not in {"http", "https"}:
            return None
        if not parts.hostname or parts.username or parts.password:
            return None
        _ = parts.port
    except ValueError:
        return None

    try:
        parsed = HttpUrl(candidate)
    except (ValidationError, ValueError, TypeError):
        return None

    # Post-canonicalization guards.
    if parsed.scheme not in {"http", "https"}:
        return None
    if not parsed.host or parsed.username or parsed.password:
        return None
    canonical = str(parsed)
    if len(canonical) > MAX_URL_CHARS:
        return None
    return canonical


class SearchBudgetExceeded(RuntimeError):
    """The ledger's search budget is exhausted, including in-flight calls."""

    def __init__(self, max_calls: int) -> None:
        self.max_calls = max_calls
        super().__init__(f"search budget exhausted (max {max_calls} calls per run)")


@dataclass
class SearchReservation:
    """One reserved search call that must be settled exactly once."""

    query: str
    _ledger: EvidenceLedger = field(repr=False)
    component_id: str | None = None
    _settled: bool = field(default=False, repr=False)


class EvidenceLedger:
    """Per-run or per-turn search evidence and search-budget authority.

    The ledger is shared by every runtime for a run or hosted turn. A search
    call reserves budget before its first await, so concurrent tool calls
    cannot all pass a limit check and then exceed the cap. Reservations are
    settled exactly once as evidence, empty, or error. Cancellation settles the
    reservation as an error and still consumes budget, so a cancelled paid call
    cannot be retried for free.
    """

    def __init__(self) -> None:
        self._evidence: dict[str, EvidenceItem] = {}
        self._by_url: dict[str, str] = {}
        self.attempts: list[SearchAttempt] = []
        self.successful_calls = 0
        self.empty_calls = 0
        self.failed_calls = 0
        self._in_flight = 0
        self._lock = threading.RLock()

    @property
    def calls(self) -> int:
        """Completed search attempts, including empty and failed ones."""
        with self._lock:
            return len(self.attempts)

    @property
    def in_flight_calls(self) -> int:
        """Reserved calls that have not been settled yet."""
        with self._lock:
            return self._in_flight

    @property
    def budget_used(self) -> int:
        """Completed attempts plus in-flight reservations."""
        with self._lock:
            return len(self.attempts) + self._in_flight

    def remaining(self, max_calls: int) -> int:
        """Budget left after completed attempts and in-flight reservations."""
        with self._lock:
            return max(0, max_calls - len(self.attempts) - self._in_flight)

    def reserve_search(
        self,
        query: str,
        max_calls: int,
        *,
        component_id: str | None = None,
    ) -> SearchReservation:
        """Atomically reserve one call before any backend await.

        Raises ``SearchBudgetExceeded`` when completed attempts plus in-flight
        reservations already reach ``max_calls``.
        """

        query_text = clean_text(query, MAX_QUERY_CHARS)
        if not query_text:
            raise ValueError("search query is empty")
        with self._lock:
            if len(self.attempts) + self._in_flight >= max_calls:
                raise SearchBudgetExceeded(max_calls)
            self._in_flight += 1
        return SearchReservation(
            query=query_text,
            component_id=component_id,
            _ledger=self,
        )

    def complete_reservation(
        self,
        reservation: SearchReservation,
        results: Sequence[SearchResult],
    ) -> list[EvidenceItem]:
        """Settle a reservation with the backend's results.

        If recording the results fails, the call is still counted as a failed
        attempt so the reservation cannot free budget after a paid call.
        """

        with self._lock:
            self._claim_locked(reservation)
            try:
                recorded = self._record_success_locked(
                    reservation.query,
                    results,
                    component_id=reservation.component_id,
                )
            except Exception:
                self._record_failure_locked(
                    reservation.query,
                    "failed to record search results",
                    component_id=reservation.component_id,
                )
                raise
            finally:
                self._in_flight -= 1
            return recorded

    def fail_reservation(self, reservation: SearchReservation, error: object) -> None:
        """Settle a reservation as a failed attempt."""

        with self._lock:
            self._claim_locked(reservation)
            self._in_flight -= 1
            self._record_failure_locked(
                reservation.query,
                error,
                component_id=reservation.component_id,
            )

    def cancel_reservation(
        self,
        reservation: SearchReservation,
        reason: object = "search call was cancelled",
    ) -> None:
        """Settle a cancelled reservation as a failed, budget-consuming call."""

        self.fail_reservation(reservation, reason)

    def _claim_locked(self, reservation: SearchReservation) -> None:
        if reservation._ledger is not self:
            raise RuntimeError("search reservation belongs to a different ledger")
        if reservation._settled:
            raise RuntimeError("search reservation was already settled")
        reservation._settled = True

    @property
    def evidence(self) -> tuple[EvidenceItem, ...]:
        with self._lock:
            return tuple(
                sorted(self._evidence.values(), key=lambda item: int(item.evidence_id[1:]))
            )

    @property
    def evidence_ids(self) -> set[str]:
        with self._lock:
            return set(self._evidence)

    def get(self, evidence_id: str) -> EvidenceItem | None:
        with self._lock:
            return self._evidence.get(evidence_id)

    def urls_for(self, evidence_ids: Iterable[str]) -> set[str]:
        with self._lock:
            return {
                self._evidence[evidence_id].url
                for evidence_id in evidence_ids
                if evidence_id in self._evidence
            }

    def record_success(
        self,
        query: str,
        results: Sequence[SearchResult],
        *,
        component_id: str | None = None,
    ) -> list[EvidenceItem]:
        """Record a completed call directly, without a reservation.

        Used by fixtures and tests. Runtime searches use ``reserve_search`` and
        ``complete_reservation`` so the budget check happens before the await.
        """

        with self._lock:
            return self._record_success_locked(
                query,
                results,
                component_id=component_id,
            )

    def _record_success_locked(
        self,
        query: str,
        results: Sequence[SearchResult],
        *,
        component_id: str | None = None,
    ) -> list[EvidenceItem]:
        """Record one backend call that returned without raising.

        Invalid result URLs are ignored. Repeated URLs reuse their evidence ID
        so the model sees one stable citation per source.
        """

        query_text = clean_text(query, MAX_QUERY_CHARS)
        recorded: list[EvidenceItem] = []
        seen_recorded: set[str] = set()
        for result in results:
            url = normalize_url(result.url)
            if url is None:
                continue
            existing_id = self._by_url.get(url)
            if existing_id is not None:
                if existing_id not in seen_recorded:
                    recorded.append(self._evidence[existing_id])
                    seen_recorded.add(existing_id)
                continue
            evidence_id = f"E{len(self._evidence) + 1}"
            item = EvidenceItem(
                evidence_id=evidence_id,
                query=query_text,
                title=clean_text(result.title, MAX_TITLE_CHARS) or urlsplit(url).hostname or url,
                url=url,
                snippet=clean_text(result.snippet, MAX_SNIPPET_CHARS),
            )
            self._evidence[evidence_id] = item
            self._by_url[url] = evidence_id
            recorded.append(item)
            seen_recorded.add(evidence_id)

        if recorded:
            self.successful_calls += 1
            outcome: Literal["evidence", "empty"] = "evidence"
        else:
            self.empty_calls += 1
            outcome = "empty"
        self.attempts.append(
            SearchAttempt(
                query=query_text,
                outcome=outcome,
                component_id=component_id,
                evidence_ids=[item.evidence_id for item in recorded],
            )
        )
        return recorded

    def record_failure(
        self,
        query: str,
        error: object,
        *,
        component_id: str | None = None,
    ) -> None:
        """Record a failed call directly, without a reservation."""

        with self._lock:
            self._record_failure_locked(query, error, component_id=component_id)

    def _record_failure_locked(
        self,
        query: str,
        error: object,
        *,
        component_id: str | None = None,
    ) -> None:
        self.failed_calls += 1
        self.attempts.append(
            SearchAttempt(
                query=clean_text(query, MAX_QUERY_CHARS),
                outcome="error",
                component_id=component_id,
                error=clean_text(error, MAX_ERROR_CHARS) or "search failed",
            )
        )

    @property
    def successful_component_ids(self) -> set[str]:
        """Components with at least one evidence-bearing search attempt.

        This is a mechanical record that a search was made for the component.
        It does not prove that the component is unaffected or that an advisory
        applies to the deployed build, configuration, or code path.
        """

        with self._lock:
            return {
                attempt.component_id
                for attempt in self.attempts
                if attempt.outcome == "evidence" and attempt.component_id
            }

    @property
    def attempted_component_ids(self) -> set[str]:
        """Components with any completed search attempt, including failures."""

        with self._lock:
            return {
                attempt.component_id
                for attempt in self.attempts
                if attempt.component_id
            }

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "calls": len(self.attempts),
                "in_flight_calls": self._in_flight,
                "budget_used": len(self.attempts) + self._in_flight,
                "successful_calls": self.successful_calls,
                "empty_calls": self.empty_calls,
                "failed_calls": self.failed_calls,
                "searched_component_ids": sorted(self.successful_component_ids),
                "attempted_component_ids": sorted(self.attempted_component_ids),
                "attempts": [attempt.as_dict() for attempt in self.attempts],
                "evidence": [item.as_dict() for item in self.evidence],
            }


class LedgerRegistry:
    """Per-turn ledgers for a long-lived hosted agent process.

    Local CLI runs use one explicit ledger and register it for their session.
    A future hosted inventory binding can use this registry so evidence from
    one turn is never visible to another turn. It is in-memory only and is not
    durable deployment state.
    """

    def __init__(self) -> None:
        self._ledgers: dict[tuple[str, str], EvidenceLedger] = {}
        self._lock = threading.Lock()

    def for_turn(self, session_id: str, turn_id: str) -> EvidenceLedger:
        key = (session_id, turn_id)
        with self._lock:
            ledger = self._ledgers.get(key)
            if ledger is None:
                ledger = EvidenceLedger()
                self._ledgers[key] = ledger
            return ledger
