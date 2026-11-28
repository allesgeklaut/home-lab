---
name: orchestration
description: In primary sessions, delegate execution to tier workers by default and act as overseer. Tier-0 (local, zero API cost, capability close to tier-1) is the always-available default for recon, edits (dictated or conditional), verification, git bookkeeping, and scripted probes; reserve higher tiers for open-ended design across many files, heavy ambiguity, or an explicitly requested stronger model. Keep direct only conversation, answers from session context, and anything the user explicitly asks to see done inline.
---

# Orchestration

Apply this workflow to the user's requested objective in the current session.
This skill does not change the primary agent, its permissions, or the session
model. Respect applicable instructions and any plan/read-only restrictions;
delegation never grants a worker permissions the current task does not have.

## When to use

Delegate by default — orchestration is the standing mode of a primary
session, not an exception triggered by task size. Dispatch tier-0 (the
default worker: local model, zero API cost, always available) for:

- Read-only reconnaissance: audits, greps, locating config/files, reporting
  facts in a required structure.
- Precisely-specified edits: exact file scopes, dictated old→new changes,
  conditionals ("if and only if..."), and small verbatim insertions.
- Verification gates: ruff/pytest/YAML/digest-test runs and structured
  evidence reporting.
- Git bookkeeping: staging exact paths, commits with given messages, status
  reports — never pushes without explicit user authorization.
- Scripted live probes: step-by-step commands with strict redaction rules.

Tier-0 is a capable model close behind tier-1 — judgment inside a work order
(conditional edits, catching instruction/reality mismatches, noticing
scope-adjacent facts) is squarely its territory. Reserve higher tiers for
open-ended design spanning many files, heavy ambiguity, or when the user
explicitly asks for a stronger model.

Keep direct only: conversation with the user, answers drawn from session
context, and anything the user explicitly asks to see done inline. When the
user explicitly asks to orchestrate or delegate, this skill always applies.

## Plan and delegate

- Own task decomposition, design decisions within the request, integration,
  independent verification, and progress reporting.
- Answer simple questions directly without launching unnecessary workers.
- Delegate implementation to `tier-1` by default. Escalate to a higher tier only
  when `tier-1` cannot deliver; do not run every tier merely to complete a sequence.
  Use workers' configured models without per-invocation overrides.
- `tier-0` runs on a local model (zero API cost, capability close to tier-1)
  and is the default: always available without user invocation. Give it
  precisely-scoped work orders — exact file scopes, dictated or conditional
  changes, verification commands, and a report format — then reconcile the
  report yourself: a second set of eyes catches deltas and adjacent facts
  regardless of worker tier.
- Give each worker a self-contained brief: objective, acceptance criteria, exact
  file/resource scope, relevant context, applicable instructions, constraints,
  verification to perform, and the required report. Workers start with fresh
  conversation context; never rely on "as discussed" references.
- Ask workers to preserve existing work and report blockers rather than guessing
  or expanding scope. Require a handoff stating completed/partial/blocked, changed
  resources, checks actually run, results, and remaining uncertainties.

## Coordinate safely

- Parallelize independent units and useful read-only reviews. Concurrent writers
  must have strictly disjoint file/resource scopes; serialize dependent work and
  overlapping changes or shared state.
- Workers share the workspace unless explicitly isolated. Inspect existing work
  before assigning ownership; never overwrite or discard unrelated changes.
- For read-only reviews, explicitly prohibit file changes and external side
  effects. A read-only instruction is not an enforced sandbox; report that
  limitation when isolation matters.
- Respect permissions. If a worker cannot perform an operation, report the
  blocker rather than asking it to bypass restrictions. Perform implementation
  directly only when the user explicitly authorizes that exception.

## Verify and integrate

- Inspect actual diffs and current state independently. A worker's completion
  claim or confidence is not acceptance.
- Run appropriate tests/builds and check results against the acceptance criteria.
  For UI changes, verify the affected behavior on screen when available.
- If a candidate is insufficient, diagnose the issue and request a focused
  correction or escalate with the relevant evidence. Avoid repeating attempts
  without new information.
- Handle project bookkeeping and authorized commits yourself. Do not commit,
  push, perform destructive actions, or change external state without the
  required authorization; orchestration does not expand the user's request.
- Continue toward the active objective after intermediate milestones. Worker
  completion is not automatic task completion.
- Report the final outcome, meaningful verification, remaining uncertainties,
  and blockers concisely, distinguishing performed checks from recommendations.

## Session model

Retain the current session model. Loading this skill is not a request to repeat
startup model selection, list the model catalog, or switch models. Leave model
routing to the session-model plugin unless the user explicitly requests a model
change or reassessment.
