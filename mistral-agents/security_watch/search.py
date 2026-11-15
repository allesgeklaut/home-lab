"""Search backends.

The production-shaped adapter is a transparent bridge to the legacy hosted
Conversations web_search tool. The outer agent uses Large 4. The hosted helper
uses Small 4: the earlier DeepSeek outer model rejected builtin connectors
(code 3004), and Small 4 was verified for hosted web_search in an isolated
probe. The adapter is injectable so tests and the offline demo never need
credentials or network access.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Protocol

from .evidence import (
    MAX_QUERY_CHARS,
    SearchResult,
    clean_text,
    normalize_url,
    redact_text,
)

if TYPE_CHECKING:
    from .usage import UsageMeter

# The outer agent uses Large 4. The hosted Conversations helper uses Small 4:
# the earlier DeepSeek outer model rejected builtin connectors (provider code
# 3004), and Small 4 was verified for hosted web_search in an isolated probe.
HOSTED_SEARCH_MODEL = "mistral-small-2603"
HOSTED_SEARCH_MAX_OUTPUT_TOKENS = 1024
HOSTED_SEARCH_TIMEOUT_SECONDS = 60.0
HOSTED_SEARCH_MAX_RESULTS = 5


class SearchBackendError(RuntimeError):
    """A search backend call failed or returned no usable evidence."""


class SearchBackend(Protocol):
    async def search(self, query: str) -> list[SearchResult]:
        """Return public advisory results for a bounded query string."""


class FakeSearchBackend:
    """Deterministic backend for the offline demo and tests."""

    def __init__(
        self,
        results: Sequence[SearchResult] = (),
        *,
        results_by_query: Mapping[str, Sequence[SearchResult]] | None = None,
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.results = list(results)
        self.results_by_query = (
            {query: list(items) for query, items in results_by_query.items()}
            if results_by_query is not None
            else None
        )
        self.error = error
        self.delay = delay
        self.queries: list[str] = []

    async def search(self, query: str) -> list[SearchResult]:
        self.queries.append(query)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        if self.results_by_query is not None:
            return list(self.results_by_query.get(query, self.results))
        return list(self.results)


def _no_retry_config() -> Any:
    """A generated-client retry config that performs exactly one attempt."""

    from mistralai.client.utils.retries import BackoffStrategy, RetryConfig

    return RetryConfig(
        strategy="none",
        backoff=BackoffStrategy(
            initial_interval=100,
            max_interval=100,
            exponent=1.0,
            max_elapsed_time=0,
        ),
        retry_connection_errors=False,
    )


def _redact(value: object) -> str:
    return redact_text(value, 400)


class HostedSearchBridge:
    """Bridge to the legacy hosted Conversations ``web_search`` tool.

    This is intentionally a development bridge, not a production search
    platform. It uses the verified Small 4 helper model for hosted web_search;
    the earlier DeepSeek outer model rejected builtin connectors (code 3004).
    It stores nothing, makes one attempt with no retries, bounds wall time and
    output tokens, and extracts citations only from tool execution metadata and
    tool_reference chunks. It never parses the model's answer text for URLs and
    accepts no arbitrary endpoint from the model.
    """

    def __init__(
        self,
        *,
        client: Any | None = None,
        model: str = HOSTED_SEARCH_MODEL,
        max_output_tokens: int = HOSTED_SEARCH_MAX_OUTPUT_TOKENS,
        timeout_seconds: float = HOSTED_SEARCH_TIMEOUT_SECONDS,
        max_results: int = HOSTED_SEARCH_MAX_RESULTS,
        meter: "UsageMeter | None" = None,
    ) -> None:
        if not 1 <= max_output_tokens <= 4096:
            raise ValueError("max_output_tokens must be between 1 and 4096")
        if not 1 <= timeout_seconds <= 300:
            raise ValueError("timeout_seconds must be between 1 and 300")
        if not 1 <= max_results <= 10:
            raise ValueError("max_results must be between 1 and 10")
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.timeout_seconds = timeout_seconds
        self.max_results = max_results
        self._client = client
        self._meter = meter

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        api_key = os.environ.get("MISTRAL_API_KEY")
        if not api_key:
            raise SearchBackendError(
                "MISTRAL_API_KEY is not set; live mode requires it via the environment or --env-file"
            )
        from mistralai.client import Mistral

        self._client = Mistral(
            api_key=api_key,
            timeout_ms=int(self.timeout_seconds * 1000),
            retry_config=_no_retry_config(),
        )
        return self._client

    async def search(self, query: str) -> list[SearchResult]:
        query_text = clean_text(query, MAX_QUERY_CHARS)
        if not query_text:
            raise SearchBackendError("search query is empty")
        client = self._get_client()
        try:
            response = await client.beta.conversations.start_async(
                model=self.model,
                inputs=query_text,
                tools=[{"type": "web_search"}],
                store=False,
                completion_args={
                    "max_tokens": self.max_output_tokens,
                    "temperature": 0,
                    "reasoning_effort": "none",
                },
                retries=_no_retry_config(),
                timeout_ms=int(self.timeout_seconds * 1000),
            )
        except asyncio.CancelledError:
            if self._meter is not None:
                self._meter.record_search_failure(cancelled=True)
            raise
        except SearchBackendError:
            if self._meter is not None:
                self._meter.record_search_failure()
            raise
        except Exception as error:  # noqa: BLE001 - bridge boundary
            if self._meter is not None:
                self._meter.record_search_failure()
            raise SearchBackendError(
                f"hosted search failed: {type(error).__name__}: {_redact(error)}"
            ) from None
        if self._meter is not None:
            self._meter.record_search_response(response, model=self.model)
        return extract_hosted_search_evidence(response, max_results=self.max_results)


def _as_mapping(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "model_dump"):
        try:
            dumped = value.model_dump(mode="json", by_alias=True, exclude_none=True)
        except Exception:  # noqa: BLE001 - defensive extraction
            return {}
        if isinstance(dumped, Mapping):
            return dumped
    if hasattr(value, "__dict__"):
        return vars(value)
    return {}


def _get(value: object, key: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _is_unset(value: object) -> bool:
    return type(value).__name__ == "Unset"


def _text_field(mapping: Mapping[str, Any], names: Sequence[str]) -> str:
    for name in names:
        value = mapping.get(name)
        if value is None or _is_unset(value):
            continue
        text = clean_text(value, 600)
        if text:
            return text
    return ""


def _walk_tool_metadata(
    value: object,
    *,
    title: str = "",
    snippet: str = "",
    found: list[SearchResult],
) -> None:
    """Walk only tool execution metadata, never assistant answer text."""

    if isinstance(value, Mapping):
        title = _text_field(value, ("title", "name", "headline")) or title
        snippet = _text_field(value, ("snippet", "description", "summary", "content")) or snippet
        url = normalize_url(value.get("url") or value.get("link"))
        if url is not None:
            found.append(SearchResult(title=title, url=url, snippet=snippet))
        for nested in value.values():
            _walk_tool_metadata(nested, title=title, snippet=snippet, found=found)
        return
    if isinstance(value, (list, tuple)):
        for nested in value:
            _walk_tool_metadata(nested, title=title, snippet=snippet, found=found)
        return
    if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return
        _walk_tool_metadata(parsed, title=title, snippet=snippet, found=found)


def extract_hosted_search_evidence(
    response: object,
    *,
    max_results: int = HOSTED_SEARCH_MAX_RESULTS,
) -> list[SearchResult]:
    """Extract citation URLs from hosted tool metadata only.

    Allowed sources:
    * ``tool.execution`` entries' ``info`` metadata (recursively, including
      JSON-encoded result payloads);
    * ``tool_reference`` content chunks inside ``message.output`` entries.

    Text chunks are deliberately ignored, even when they contain URLs.
    """

    outputs = _get(response, "outputs", [])
    if not isinstance(outputs, (list, tuple)):
        return []
    found: list[SearchResult] = []
    for entry in outputs:
        entry_type = _get(entry, "type")
        if entry_type == "tool.execution":
            _walk_tool_metadata(_get(entry, "info"), found=found)
        elif entry_type == "message.output":
            content = _get(entry, "content", [])
            if isinstance(content, str):
                continue
            if not isinstance(content, (list, tuple)):
                continue
            for chunk in content:
                if _get(chunk, "type") != "tool_reference":
                    continue
                mapping = _as_mapping(chunk)
                url = normalize_url(mapping.get("url"))
                if url is None:
                    continue
                found.append(
                    SearchResult(
                        title=_text_field(mapping, ("title", "name")),
                        url=url,
                        snippet=_text_field(mapping, ("description", "snippet")),
                    )
                )

    deduped: list[SearchResult] = []
    seen: set[str] = set()
    for result in found:
        url = normalize_url(result.url)
        if url is None or url in seen:
            continue
        seen.add(url)
        deduped.append(
            SearchResult(
                title=clean_text(result.title, 200),
                url=url,
                snippet=clean_text(result.snippet, 600),
            )
        )
        if len(deduped) >= max_results:
            break
    return deduped
