"""Command-line entry point for the local Security Watch prototype."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

from .agent_factory import (
    DEFAULT_MAX_ITERATIONS,
    MAX_ITERATIONS_CEILING,
    build_agent,
)
from .demo import demo_backend, demo_scripted_model
from .evidence import redact_text
from .inventory import InventoryError, load_inventory
from .runner import RunResult, run_security_watch
from .runtime import (
    DEFAULT_MAX_SEARCH_CALLS,
    MAX_RESULTS_PER_CALL,
    MAX_SEARCH_CALLS_CEILING,
    make_runtime,
)
from .sdk_compat import disable_model_catalogue_lookup
from .search import HostedSearchBridge
from .usage import UsageHook, UsageMeter

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INVENTORY = PROJECT_ROOT / "fixtures" / "inventory.json"
MAX_ENV_FILE_BYTES = 64 * 1024
MAX_TIMEOUT_SECONDS = 1800.0
ENV_KEY = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def _redact(value: object) -> str:
    return redact_text(value, 500)


def load_env_file(path: Path) -> None:
    """Load KEY=VALUE pairs without printing or overriding existing values."""

    try:
        size = path.stat().st_size
    except OSError as error:
        raise ValueError(f"cannot read env file: {error.strerror or error}") from None
    if size > MAX_ENV_FILE_BYTES:
        raise ValueError("env file exceeds the local 64 KiB limit")
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        mode = 0
    if mode & 0o077:
        print(
            "warning: env file is readable by group or other users",
            file=sys.stderr,
        )
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise ValueError(f"cannot read env file: {_redact(error)}") from None
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].strip()
        if "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if not ENV_KEY.fullmatch(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="security-watch",
        description=(
            "Run the Security Watch agent on a sanitized inventory. "
            "The default is an offline SDK demo with a fake search backend."
        ),
    )
    parser.add_argument(
        "--inventory",
        default=str(DEFAULT_INVENTORY),
        help="sanitized inventory JSON path, or - for stdin (default: fixtures/inventory.json)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "use the real mistral-large-4 agent and hosted Small 4 web_search "
            "bridge (paid calls)"
        ),
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="load KEY=VALUE values for --live, e.g. /opt/secrets/mistral.env",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(".state"),
        help="local per-run audit directory (default: .state)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        help=(
            "wall-clock seconds for the turn "
            f"(default 180, ceiling {MAX_TIMEOUT_SECONDS:g})"
        ),
    )
    parser.add_argument(
        "--max-search-calls",
        type=int,
        default=DEFAULT_MAX_SEARCH_CALLS,
        help=(
            "search calls per run "
            f"(default {DEFAULT_MAX_SEARCH_CALLS}, ceiling {MAX_SEARCH_CALLS_CEILING})"
        ),
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=DEFAULT_MAX_ITERATIONS,
        help=(
            "agent loop steps "
            f"(default {DEFAULT_MAX_ITERATIONS}, ceiling {MAX_ITERATIONS_CEILING})"
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the structured result instead of the human summary",
    )
    return parser


def _print_human(result: RunResult, state_path: Path | None) -> None:
    print(f"Security Watch: {result.status}")
    print(f"Run: {result.run_id} ({result.duration_seconds:.3f}s)")
    if result.report is not None:
        report = result.report
        print(f"Assessment: {report.assessment}")
        print(f"Summary: {report.summary}")
        print(f"Findings: {len(report.findings)}")
        checked = ", ".join(report.checked_component_ids) or "none"
        print(f"Checked components: {checked}")
        for finding in report.findings:
            print(
                f"- [{finding.severity}/{finding.status}] "
                f"{finding.component_id}: {finding.title}"
            )
            for evidence_id in finding.evidence_ids:
                item = result.ledger.get(evidence_id)
                if item is not None:
                    print(f"  Evidence: {evidence_id} {item.url}")
        print("Limitations:")
        for limitation in report.limitations:
            print(f"- {limitation}")
    if result.error:
        print(f"Error: {result.error}")
    print(
        "Searches: "
        f"{result.ledger.successful_calls} with evidence, "
        f"{result.ledger.empty_calls} empty, "
        f"{result.ledger.failed_calls} failed"
    )
    if result.usage:
        outer = result.usage["outer"]
        search = result.usage["search"]
        estimate = result.usage["model_token_estimate_usd"]["total"]
        estimate_text = (
            f"${estimate:.4f}"
            if isinstance(estimate, (int, float))
            else "unavailable (unknown search-model pricing)"
        )
        print(
            "Usage: "
            f"outer {outer['calls']} calls "
            f"({outer['calls_without_usage']} without usage), "
            f"hosted search {search['calls']} calls; "
            f"partial model-token estimate {estimate_text}; "
            "excludes connector/search fees"
        )
    if state_path is not None:
        print(f"State: {state_path}")


def _print_json(result: RunResult, state_path: Path | None) -> None:
    payload = result.as_dict()
    payload["state_path"] = str(state_path) if state_path is not None else None
    print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    if args.env_file is not None and not args.live:
        parser.error("--env-file is only valid with --live")
    if args.timeout <= 0 or args.timeout > MAX_TIMEOUT_SECONDS:
        parser.error(
            f"--timeout must be between 1 and {MAX_TIMEOUT_SECONDS:g} seconds"
        )

    effective_search_calls = max(
        1, min(args.max_search_calls, MAX_SEARCH_CALLS_CEILING)
    )
    effective_iterations = max(
        1, min(args.max_iterations, MAX_ITERATIONS_CEILING)
    )
    if args.max_search_calls > MAX_SEARCH_CALLS_CEILING:
        print(
            f"warning: --max-search-calls hard-capped at {MAX_SEARCH_CALLS_CEILING}",
            file=sys.stderr,
        )
    elif args.max_search_calls < 1:
        print("warning: --max-search-calls raised to 1", file=sys.stderr)
    if args.max_iterations > MAX_ITERATIONS_CEILING:
        print(
            f"warning: --max-iterations hard-capped at {MAX_ITERATIONS_CEILING}",
            file=sys.stderr,
        )
    elif args.max_iterations < 1:
        print("warning: --max-iterations raised to 1", file=sys.stderr)

    try:
        inventory = load_inventory(args.inventory)
    except InventoryError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    meter = UsageMeter()
    if args.live:
        if args.env_file is not None:
            try:
                load_env_file(args.env_file)
            except ValueError as error:
                print(f"error: {error}", file=sys.stderr)
                return 2
        if not os.environ.get("MISTRAL_API_KEY"):
            print(
                "error: --live requires MISTRAL_API_KEY via the environment or --env-file",
                file=sys.stderr,
            )
            return 2
        print(
            "warning: live mode makes paid Mistral model and hosted web_search calls. "
            "Loop/search caps are local; total token spend is not hard-capped.",
            file=sys.stderr,
        )
        backend = HostedSearchBridge(meter=meter)
        agent = build_agent(
            max_iterations=effective_iterations, hooks=[UsageHook(meter)]
        )
    else:
        backend = demo_backend()
        disable_model_catalogue_lookup()
        agent = build_agent(
            max_iterations=effective_iterations, hooks=[UsageHook(meter)]
        ).override(model=demo_scripted_model())

    runtime = make_runtime(
        backend,
        inventory=inventory,
        max_search_calls=effective_search_calls,
        max_results_per_call=MAX_RESULTS_PER_CALL,
    )
    try:
        result = asyncio.run(
            run_security_watch(
                agent,
                inventory,
                runtime=runtime,
                timeout=args.timeout,
                max_iterations=effective_iterations,
                state_dir=args.state_dir,
                usage=meter,
            )
        )
    except Exception as error:  # noqa: BLE001 - CLI boundary
        print(f"error: {_redact(error)}", file=sys.stderr)
        return 1

    state_path = args.state_dir / "runs" / result.run_id
    if args.json:
        _print_json(result, state_path)
    else:
        _print_human(result, state_path)
    return 0 if result.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
