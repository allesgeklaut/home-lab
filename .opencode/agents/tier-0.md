---
description: Tier 0 (local) worker on the local Qwen 3.8 Flash Next model (served by Strata — no API cost, no Go allowance). The default delegation target — always available without user invocation; escalate to higher tiers for open-ended design, heavy ambiguity, or when the user explicitly asks for a stronger model.
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
