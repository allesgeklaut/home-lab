"""Run one Security Watch turn with stop-reason, timeout, and validation checks."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from .sdk_compat import prepare_sdk_environment

prepare_sdk_environment()

from mistralai import agents  # noqa: E402

from .evidence import EvidenceLedger, clean_text, redact_text  # noqa: E402
from .models import Inventory, SecurityReport  # noqa: E402
from .runtime import RUNTIME_REGISTRY, ToolRuntime  # noqa: E402
from .usage import UsageMeter  # noqa: E402
from .validation import ReportValidationError, validate_report  # noqa: E402

MAX_PAYLOAD_BYTES = 512 * 1024
MAX_MODEL_TEXT_CHARS = 20_000
MAX_TOOL_ARGUMENT_CHARS = 1_000
MAX_ERROR_CHARS = 500
CANCEL_GRACE_SECONDS = 5.0

RunStatus = Literal["completed", "rejected", "timeout", "failed"]


@dataclass
class RunResult:
    status: RunStatus
    run_id: str
    started_at: str
    finished_at: str
    duration_seconds: float
    ledger: EvidenceLedger
    report: SecurityReport | None = None
    error: str | None = None
    stop_reason: str | None = None
    model_text: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    limits: dict[str, int] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "report": self.report.model_dump(mode="json") if self.report else None,
            "error": self.error,
            "stop_reason": self.stop_reason,
            "tool_calls": self.tool_calls,
            "limits": self.limits,
            "usage": self.usage,
            "ledger": self.ledger.snapshot(),
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def _safe_error(value: object) -> str:
    text = redact_text(value, MAX_ERROR_CHARS) or type(value).__name__
    return text


def _tool_call_summary(state: object) -> list[dict[str, Any]]:
    calls = getattr(state, "tool_calls", ()) or ()
    summary: list[dict[str, Any]] = []
    for call in calls:
        arguments = getattr(call, "arguments", None)
        try:
            rendered = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            rendered = str(arguments)
        summary.append(
            {
                "name": clean_text(getattr(call, "name", "unknown"), 100),
                "arguments": clean_text(rendered, MAX_TOOL_ARGUMENT_CHARS),
            }
        )
    return summary


def _build_payload(inventory: Inventory, runtime: ToolRuntime) -> str:
    """Build the lean research payload.

    The report schema is supplied by the SDK ``output_type``; repeating it here
    would duplicate instructions. The payload keeps only the sanitized
    inventory, the UTC timestamp, and the enforced search limits.
    """

    payload = {
        "as_of_utc": _now(),
        "task": (
            "Assess this sanitized software inventory for public security "
            "advisories. Research first, then return the structured report."
        ),
        "inventory": inventory.model_dump(mode="json"),
        "max_search_calls": runtime.max_search_calls,
        "max_results_per_call": runtime.max_results_per_call,
    }
    encoded = json.dumps(payload, ensure_ascii=False)
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ValueError("research payload exceeds the local byte budget")
    return encoded


async def _cancel(session: agents.Session) -> None:
    try:
        await asyncio.wait_for(session.cancel(), timeout=CANCEL_GRACE_SECONDS)
    except (Exception, asyncio.CancelledError):  # noqa: BLE001 - best-effort cleanup
        pass


def _completed_result(
    *,
    state: agents.Completed,
    inventory: Inventory,
    ledger: EvidenceLedger,
    run_id: str,
    started_at: str,
    started_monotonic: float,
    limits: dict[str, int],
    usage: dict[str, Any],
) -> RunResult:
    finished_at = _now()
    duration = round(time.monotonic() - started_monotonic, 3)
    model_text = clean_text(getattr(state, "text", "") or "", MAX_MODEL_TEXT_CHARS) or None
    tool_calls = _tool_call_summary(state)

    def result(
        status: RunStatus,
        *,
        error: str | None = None,
        report: SecurityReport | None = None,
    ) -> RunResult:
        return RunResult(
            status=status,
            run_id=run_id,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=duration,
            ledger=ledger,
            report=report,
            error=error,
            stop_reason=getattr(state, "stop_reason", None),
            model_text=model_text,
            tool_calls=tool_calls,
            limits=limits,
            usage=usage,
        )

    stop_reason = getattr(state, "stop_reason", None)
    if stop_reason is not None:
        return result("rejected", error=f"turn stopped early: {stop_reason}")
    if isinstance(state.output, agents.InvalidOutput):
        return result(
            "rejected",
            error=f"model output did not match the report schema: {clean_text(state.output.error, 300)}",
        )
    if not isinstance(state.output, SecurityReport):
        return result("rejected", error="model output was not a validated SecurityReport")
    try:
        report = validate_report(state.output, inventory, ledger)
    except ReportValidationError as error:
        return result("rejected", error=f"report failed local validation: {clean_text(error, 1000)}")
    return result("completed", report=report)


async def run_security_watch(
    agent: agents.Agent,
    inventory: Inventory,
    *,
    runtime: ToolRuntime,
    timeout: float = 180.0,
    max_iterations: int = 6,
    state_dir: Path | None = None,
    run_id: str | None = None,
    usage: UsageMeter | None = None,
) -> RunResult:
    """Run one turn and return a structured, locally validated result.

    The wall-clock timeout covers the SDK turn wait. On timeout the session is
    cancelled. The runtime is registered only for this session and removed
    afterwards, so a later run cannot cite this run's evidence.
    """

    if timeout <= 0:
        raise ValueError("timeout must be positive")
    if runtime.inventory is None:
        runtime.inventory = inventory
    elif runtime.inventory != inventory:
        raise ValueError("runtime inventory does not match the run inventory")
    run_id = run_id or _new_run_id()
    started_at = _now()
    started_monotonic = time.monotonic()
    agent_iterations = getattr(agent, "max_iterations", None)
    effective_iterations = (
        int(agent_iterations) if isinstance(agent_iterations, int) else int(max_iterations)
    )
    limits = {
        "max_iterations": effective_iterations,
        "max_search_calls": runtime.max_search_calls,
        "max_results_per_call": runtime.max_results_per_call,
    }
    payload = _build_payload(inventory, runtime)

    session: agents.Session | None = None
    state: object | None = None
    timed_out = False
    failure: str | None = None
    try:
        session = await agent.start_session()
        RUNTIME_REGISTRY.register(session.id, runtime)
        try:
            async with asyncio.timeout(timeout):
                state = await session.run(payload)
        except TimeoutError:
            timed_out = True
            failure = f"wall-clock timeout after {timeout:g}s"
        except Exception as error:  # noqa: BLE001 - runner boundary
            failure = f"run failed: {_safe_error(error)}"
    finally:
        if session is not None:
            RUNTIME_REGISTRY.unregister(session.id)
            await _cancel(session)

    finished_at = _now()
    duration = round(time.monotonic() - started_monotonic, 3)
    usage_snapshot = usage.snapshot() if usage is not None else {}

    if timed_out:
        result = RunResult(
            status="timeout",
            run_id=run_id,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=duration,
            ledger=runtime.ledger,
            error=failure,
            limits=limits,
            usage=usage_snapshot,
        )
    elif failure is not None:
        result = RunResult(
            status="failed",
            run_id=run_id,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=duration,
            ledger=runtime.ledger,
            error=failure,
            limits=limits,
            usage=usage_snapshot,
        )
    elif isinstance(state, agents.Completed):
        result = _completed_result(
            state=state,
            inventory=inventory,
            ledger=runtime.ledger,
            run_id=run_id,
            started_at=started_at,
            started_monotonic=started_monotonic,
            limits=limits,
            usage=usage_snapshot,
        )
    else:
        stop_reason = getattr(state, "stop_reason", None)
        if isinstance(state, agents.Failed):
            error = f"turn failed: {_safe_error(state.error)}"
        elif isinstance(state, agents.Interrupted):
            error = f"turn interrupted: {clean_text(getattr(state, 'cause', 'unknown'), 200)}"
        elif isinstance(state, agents.Blocked):
            error = "turn blocked on a callback; no callback handler was provided"
        elif isinstance(state, agents.Running):
            error = "turn still running after the wait returned"
        elif isinstance(state, agents.Busy):
            error = "session was busy with another turn"
        elif isinstance(state, agents.UnknownOutcome):
            error = f"unknown turn outcome: {clean_text(getattr(state, 'outcome', 'unknown'), 100)}"
        else:
            error = f"unexpected turn state: {type(state).__name__}"
        result = RunResult(
            status="failed",
            run_id=run_id,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=duration,
            ledger=runtime.ledger,
            error=error,
            stop_reason=stop_reason,
            model_text=clean_text(getattr(state, "text", "") or "", MAX_MODEL_TEXT_CHARS) or None,
            tool_calls=_tool_call_summary(state),
            limits=limits,
            usage=usage_snapshot,
        )

    if state_dir is not None:
        write_run_state(result, inventory, state_dir)
    return result


def _atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    temporary = path.with_name(path.name + ".tmp")
    data = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    os.replace(temporary, path)


def write_run_state(result: RunResult, inventory: Inventory, state_dir: Path) -> Path:
    """Write one run's audit artifact under ``state_dir/runs/<run_id>``."""

    run_dir = Path(state_dir) / "runs" / result.run_id
    run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        run_dir.chmod(0o700)
    except OSError:
        pass

    run_payload = {
        "run_id": result.run_id,
        "status": result.status,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "duration_seconds": result.duration_seconds,
        "error": result.error,
        "stop_reason": result.stop_reason,
        "limits": result.limits,
        "tool_calls": result.tool_calls,
        "usage": result.usage,
        "inventory": inventory.model_dump(mode="json"),
        "report": result.report.model_dump(mode="json") if result.report else None,
        "ledger": result.ledger.snapshot(),
        "model_text_unvalidated": result.model_text,
        "validation": "local ledger-backed validation passed"
        if result.report is not None
        else "no validated report",
    }
    _atomic_write_json(run_dir / "run.json", run_payload)
    return run_dir
