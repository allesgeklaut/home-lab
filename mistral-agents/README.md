# Security Watch Agent

A small, standalone **Mistral Agents SDK** prototype for evidence-backed
security research over a sanitized software inventory. It is not a port of the
legacy `scripts/security-watch.py` runner and is not a production replacement.

- **Status:** local prototype; realistic `mistral-large-4` evaluation pending.
- **Models:** outer agent `mistral-large-4` with reasoning effort `medium`;
  hosted search helper `mistral-small-2603` (Small 4) with
  `reasoning_effort="none"`. The legacy watcher is a separate configuration.
- **Tools:** one direct research tool. No shell, sandbox, subagents, MCP,
  connectors, arbitrary HTTP fetching, or programmatic tool calling
  (`Builtins.none()`).
- **Output:** a strict `SecurityReport` with sources, evidence IDs, component
  attribution, and uncertainty. The provider wire schema is relaxed; local
  validation is unchanged.
- **Default:** offline demo using the real SDK loop with
  `agents.testing.ScriptedModel` and a fake search backend. No key, no network.
- **Live:** explicit `--live`; paid Mistral model and hosted `web_search` calls.

## Quick start

```bash
cd /opt/stacks/mistral-agents
uv sync --locked
uv run pytest
uv run python main.py --inventory fixtures/inventory.json
```

The default command validates the report and writes one audit artifact,
`.state/runs/<run_id>/run.json`, containing status, limits, inventory, the
validated report (or `null`), the ledger snapshot, the usage snapshot,
unvalidated model text, and the validation note. There are no duplicate
`report.json` or `ledger.json` files. `--json` prints the structured result.

## Live mode

```bash
uv run python main.py --live --env-file /opt/secrets/mistral.env \
  --inventory fixtures/inventory.json
```

`--env-file` loads `KEY=VALUE` pairs without printing values or overriding
already-set variables. `--live` requires `MISTRAL_API_KEY` and makes paid model
and hosted `web_search` calls. It is wired and unit-tested with a fake
Conversations client; runs are manual, with no scheduler, notification path, or
deployment. Real model/search behavior and cost remain unvalidated.

## Inventory input

The CLI accepts a sanitized JSON document, not host/SSH output:

```json
{
  "schema_version": 1,
  "environment": "synthetic-demo",
  "components": [
    {
      "id": "web-app",
      "name": "Acme Web App",
      "version": "2.4.1",
      "ecosystem": "npm",
      "notes": "Synthetic fixture; no host or customer data."
    }
  ]
}
```

Loader rules: at most **256 KiB**; strict schema with unknown fields rejected;
1–50 components; IDs match `^[a-z][a-z0-9._-]{1,63}$` and are unique; no
hostnames, paths, users, commands, or credentials are schema fields.
`fixtures/inventory.json` is synthetic; the offline demo's advisory URLs
use the reserved `.test` domain.

## What the agent does

1. Receives the sanitized inventory, a UTC timestamp, and the enforced search
   limits. The report schema comes from the SDK `output_type`
   (`WireSecurityReport`), not a duplicated prompt payload.
2. Calls `search_security_advisories` with a required `component_id` and an
   optional short focus phrase.
3. The tool validates `component_id` against the runtime's registered inventory
   before any backend call or budget reservation. It builds the query prefix
   server-side from the trusted component name, version, and ecosystem, then
   appends the bounded focus. A model-supplied focus cannot remove the
   component/version prefix.
4. Returns evidence IDs and URLs; the model produces the structured report.

Inventory fields and search results are untrusted data, not instructions. The
agent is instructed to ignore embedded requests to change its task, bypass
validation, disclose secrets, execute commands, or use tools beyond the research
tool. This is prompt-level hardening, not a substitute for host-side validation.

## Report contract

`actionable_findings` requires at least one supported, evidence-backed finding;
partial, failed, or empty coverage elsewhere does not suppress it.
`inconclusive` means no supported findings and partial, failed, empty, or
insufficient research; at least one tool/backend attempt must be recorded.
`no_actionable_findings` requires a successful evidence-bearing search for every
inventory component, `checked_component_ids` covering exactly the inventory, and
no failed, empty, or in-flight search.

For every assessment, `checked_component_ids` must be a subset of the
successfully searched components. Coverage comes from ledger attribution, not
the model's list or matching query text. Findings still require evidence IDs and
citations; URLs use the same Pydantic `HttpUrl` normalization, so non-http,
credential-bearing, overlong, and invented URLs are rejected.

A recorded search proves only that a component-attributed search was made, not
that the component is unaffected or that an advisory applies to the deployed
version, build, configuration, or code path. Applicability remains a human
review step. The default cap is four search calls per run; the explicit ceiling
is 64. With more components than search calls, a full all-clear is impossible.
Name unsearched components as gaps; if there are no supported findings, report
`inconclusive`.

## Wire schema

The SDK sends `WireSecurityReport` (`security_watch/wire.py`) as the provider
`response_format`. It is a subclass of `SecurityReport` that only overrides
`model_json_schema()`: field annotations and constraints (`title`,
`description`, `format`, `pattern`, length/array bounds, `default`) are removed
from the wire schema, while `$defs`/`$ref`, property names (including
`Finding.title`), enums, `required`, and `additionalProperties` are kept.
During the earlier DeepSeek diagnostic, the provider rejected the full
generated schema with HTTP 500; the exact single keyword is not isolated. The
SDK agent now uses `mistral-large-4`/`medium`; there is no evidence Large 4
needs the relaxation, but it is retained because it preserves the report shape
and local validation. The schema-aware relaxed shape was accepted in an
isolated probe.

Local validation is unchanged. The SDK parses the reply into
`WireSecurityReport`, which inherits every `SecurityReport` field, default, and
validator, so bad component/evidence IDs, non-http URLs, empty limitations, and
oversized strings are still rejected locally. The runner's
`isinstance(output, SecurityReport)` check and ledger-backed validation still
apply.

## Search backends

`security_watch/search.py` defines an injectable `SearchBackend` protocol.

- `FakeSearchBackend` — deterministic results for tests and the offline demo.
- `HostedSearchBridge` — legacy development bridge to the hosted Conversations
  `web_search` tool: helper model `mistral-small-2603` (Small 4), `store=False`,
  one attempt with no retries, 60-second timeout, `max_tokens=1024`,
  `reasoning_effort="none"`, at most 5 results, no model-supplied endpoint, and
  citations only from `tool.execution` metadata and `tool_reference` chunks.
  The outer agent is `mistral-large-4`; the helper is separate. Replace it
  behind the protocol when a production search service is chosen.

## Limits and gaps

| Limit | Default | Ceiling |
| --- | ---: | ---: |
| Agent loop iterations | 6 | 80 |
| Search calls per run | 4 | 64 |
| Results per search call | 5 | 5 |
| Inventory input | — | 256 KiB |
| First model output | 120 s | 120 s |
| Wall-clock turn wait | 180 s | 1800 s |
| Findings per report | — | 20 |

The CLI clamps and warns on `--max-search-calls` and `--max-iterations`;
`--timeout` must be within 1–1800 seconds. Search budget is reserved in the
shared ledger before the backend await; concurrent calls share the cap, and
cancellation consumes budget. The search cap counts local research/bridge
requests, not internal hosted `web_search` executions or total connector/search
billing. The wall-clock timeout cancels the session.

The SDK's model token budget and provider retry budget are not hard-capped. The
SDK retries a failed model call up to 5 times by default; this prototype does not
override that. Per-call `max_tokens` is not treated as a total-spend control.
There is no total token or spend guarantee.

## Usage and cost

A live run has two separately billed surfaces: the outer `mistral-large-4`
agent loop and each hosted `mistral-small-2603` search bridge call.
`security_watch/usage.py` records both through public interfaces:

- `UsageHook.post_llm_call` records outer model calls; `post_agent_turn` records
  the final answer as a cross-check only, never added to the outer total.
- `HostedSearchBridge` records `ConversationResponse.usage` and counts
  `tool.execution` entries as a proxy for hosted tool executions, not as a
  billed search count.
- Bridge failures and cancellations are counted as unknown billed usage: the
  request may have reached the provider, but its tokens are not observable.

`run.json` includes a `usage` block with per-surface token counts,
`total_tokens_reported`, connector metadata, `calls_without_usage`, and a
model-token estimate. Missing usage increments `calls_without_usage`; it is
never treated as zero.

The estimate uses separate verified rate cards, both as of 2026-11-15:

- Outer `mistral-large-4`: input $0.70/M, cached input $0.07/M, output $2.10/M;
  source https://docs.mistral.ai/models/mistral-large-4-0.md.
- Hosted search `mistral-small-2603`: input $0.15/M, output $0.60/M; source
  https://docs.mistral.ai/inference/pricing.md.

`run.json` names both models and rate cards separately in `usage.pricing`. Small
4 cached-input pricing is not verified in the pricing source; Conversations
reports no cached-token data, so reported search prompt tokens are priced at the
standard input rate. This is a partial model-token estimate, not a bill or
guaranteed lower bound: failed provider attempts, hidden compaction calls, and
usage the SDK does not report are excluded. Connector/search fees have no
verified local rate and are excluded; `connector_tokens` is reported but not
priced. `connectors` is a raw provider map whose values may be call counts, not
tokens. Conversations `total_tokens` may include connector tokens and need not
equal prompt + completion. `cache_write_tokens` has no supplied rate. If a custom
search model is injected, the meter flags search pricing unknown and does not
apply the Small 4 card. There is no budget enforcement or spend guarantee.

`main.py` exposes `agent` for SDK serve/deploy entry points. Importing it does
not read `MISTRAL_API_KEY` or start a network call. The hosted scaffold has no
inventory binding and no post-turn validation hook, so `RuntimeRegistry.resolve`
fails closed: it refuses a session-less call, a registered runtime without an
inventory binding, and an unregistered session without an inventory-bound
runtime. It does not share a default ledger as evidence. The per-turn ledger
registry is in-memory, does not evict old turns, and loses in-flight evidence on
restart. A future hosted integration needs an inventory-binding hook, the same
post-turn validation, durable per-session evidence, and an explicit deployment
review. This module is a development scaffold, not deploy-ready; hosted
serve/deploy was not executed.

Not included: durable sessions/evidence, scheduling, notifications,
authentication/authorization/rate limiting, a production search provider or SLA,
invoice-grade cost accounting or spend alerts, Secret Manager integration, or a
Dockerfile.

## Environment notes

- Python 3.12.3 pinned in `.python-version`; `requires-python >=3.12,<3.15`.
- SDK `mistralai-agents==1.1.3`, pinned; `uv.lock` included. Usage fields were
  verified against `mistralai-agents==1.1.3` and `mistralai==3.7.0`; other
  versions may differ.
- SDK 1.1.3 reads a bare `AGENT` value as nested worker configuration; the
  compatibility shim removes only non-object values. The offline demo/tests also
  disable the optional model-metadata HTTP lookup. Neither changes token limits
  or retry behavior.
- The SDK completion config defaults to `MISTRAL_CLIENT_API_KEY`; the CLI and
  hosted bridge use `MISTRAL_API_KEY`. `MISTRAL_API_URL`, `MISTRAL_SERVER_URL`,
  or `SERVER_URL` can reroute completions. This is a setup-hardening gap, not
  the cause of the schema 500; pinning `MistralCompletionConfig` with an explicit
  `api_key_env_var` and `base_url` is left for a future change.

## Tests

```bash
uv run --locked pytest -o addopts='' --tb=short
```

Offline tests use `no_network` socket guards and disable the SDK catalogue
lookup. They cover the SDK loop, structured coverage, partial findings,
zero-evidence inconclusive, URL canonicalization, budget/concurrency,
cancellation, hosted bridge settings, usage accounting (missing usage, subsets,
separate surfaces, bridge failures, hook accumulation, artifact snapshots),
inventory bounds, and CLI artifacts. The wire-schema tests assert that
`WireSecurityReport` strips only the incompatible annotation/constraint group,
keeps `Finding.title` and the full required/property/enum/`$ref` shape, and
still rejects bad IDs, bad URLs, empty limitations, and oversized strings. A
real Agent serialization test drives public `start_session`/`session.run` with a
fake HTTP send, captures the actual outgoing `response_format`, and asserts
`reasoning_effort="medium"` and the relaxed schema. Usage tests assert that the
outer and search surfaces use different rate cards and that a custom search
model is flagged unknown instead of being priced as Small 4. No test requires
credentials or makes a paid call.

## References

- [Move from the Agents API](https://docs.mistral.ai/studio/agents/getting-started/from-agents-api) — hosted-tool bridge pattern used by `HostedSearchBridge`.
- [Deploy your agent](https://docs.mistral.ai/studio/agents/getting-started/deploy-it) — official serve/deploy path.
- [Agent reference](https://docs.mistral.ai/studio/agents/reference/agent) — `Agent`, `Builtins`, limits, and hooks.
