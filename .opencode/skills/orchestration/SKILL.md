---
name: Orchestration
description: In primary sessions, coordinate tier workers automatically for complex tasks where delegation materially helps, such as substantial independent workstreams, multi-component implementation, or investigation needing separate exploration and verification. Also use when the user explicitly asks to orchestrate or delegate. Keep simple questions and small, well-understood edits direct.
---

# Orchestration

Apply this workflow to the user's requested objective in the current session.
This skill does not change the primary agent, its permissions, or the session
model. Respect applicable instructions and any plan/read-only restrictions;
delegation never grants a worker permissions the current task does not have.

## When to use

- In a primary session, load this skill without waiting for an explicit request
  when the task is complex and delegation offers a concrete benefit: substantial
  independent workstreams, context-heavy investigation, or separate implementation
  and verification work.
- Complexity alone is not a reason to spawn workers. Keep simple questions,
  small well-understood edits, and work whose delegation overhead outweighs the
  benefit direct. Respect a user request not to delegate.
- Apply orchestration only to the active objective, not every later turn.
  Workers execute their assigned briefs; they must not recursively activate
  this workflow or spawn further workers.

## Plan and delegate

- Own task decomposition, design decisions within the request, integration,
  independent verification, and progress reporting.
- Answer simple questions directly without launching unnecessary workers.
- Delegate implementation to `tier-1` by default. Escalate to a higher tier only
  when `tier-1` cannot deliver; do not run every tier merely to complete a sequence.
  Use workers' configured models without per-invocation overrides.
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
