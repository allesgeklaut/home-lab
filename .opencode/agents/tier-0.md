---
description: Tier 0 (local) worker on the local Qwen 3.8 Flash Next model (served by Strata — no API cost, no Go allowance; capability close to tier-1). The default delegation target — always available without user invocation. Handles precisely-specified edits and work orders that leave room for judgment: conditional changes, verifying instructions against real code behavior, adapting when the two collide, and reporting deviations with reasons. Typical orders: read-only recon and investigation, exact-file edits, verification gates (ruff/pytest/YAML/digest tests), git bookkeeping (staging, commits with given messages), and scripted live probes. The primary agent designs work orders and reconciles reports as a second set of eyes; reserve higher tiers for open-ended design across many files, heavy ambiguity, or an explicitly requested stronger model.
mode: subagent
model: llama-cpp/qwen3.8-flash-next#xhigh
permissions:
  - action: subagent
    resource: "*"
    effect: deny
  - action: question
    resource: "*"
    effect: deny
---
