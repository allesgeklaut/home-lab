from __future__ import annotations

import importlib

from security_watch.agent_factory import (
    DEFAULT_MAX_ITERATIONS,
    FIRST_OUTPUT_TIMEOUT_SECONDS,
    MAX_ITERATIONS_CEILING,
    MODEL,
    build_agent,
)
from security_watch.models import SecurityReport
from security_watch.wire import WireSecurityReport

from mistralai import agents


def test_agent_configuration_is_focused() -> None:
    agent = build_agent()
    assert agent.model == MODEL
    assert agent.name == "security-watch"
    assert agent.reasoning_effort == "medium"
    assert agent.max_iterations == DEFAULT_MAX_ITERATIONS
    assert agent.first_output_timeout_seconds == FIRST_OUTPUT_TIMEOUT_SECONDS
    assert agent.output_type is WireSecurityReport
    assert issubclass(WireSecurityReport, SecurityReport)
    assert agent.subagents == []
    assert agent.builtins == agents.Builtins.none()
    assert agent.tool_errors == "hide"


def test_agent_loop_limit_is_hard_capped() -> None:
    assert build_agent(max_iterations=999).max_iterations == MAX_ITERATIONS_CEILING
    assert build_agent(max_iterations=0).max_iterations == 1


def test_instructions_treat_inventory_and_search_as_untrusted() -> None:
    instructions = build_agent().instructions
    assert "untrusted data" in instructions
    assert "not instructions" in instructions


def test_instructions_cover_component_coverage_and_inconclusive() -> None:
    instructions = build_agent().instructions
    assert "component_id" in instructions
    assert "checked_component_ids" in instructions
    assert "no_actionable_findings" in instructions
    assert "inconclusive" in instructions
    assert "search attempt" in instructions


def test_instructions_allow_supported_findings_with_partial_coverage() -> None:
    instructions = " ".join(build_agent().instructions.split())
    assert (
        "actionable_findings requires at least one supported, evidence-backed finding"
        in instructions
    )
    assert "does not suppress a supported finding" in instructions
    assert "Name only the actually researched component IDs" in instructions
    assert "inconclusive means there are no supported findings" in instructions
    assert "Do not put findings on an inconclusive report" in instructions


def test_instructions_prioritize_large_inventory_compactness() -> None:
    instructions = " ".join(build_agent().instructions.split())
    assert "prioritize the most consequential components" in instructions
    assert "at most 40 evidence IDs" in instructions
    assert "at most 10 limitations" in instructions


def test_main_import_does_not_need_a_key_or_network(monkeypatch, no_network) -> None:
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    module = importlib.import_module("main")
    assert isinstance(module.agent, agents.Agent)
    assert module.agent.model == MODEL
