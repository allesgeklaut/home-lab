---
description: Tier 0 (local) worker on the local Qwen 3.8 Flash Next model (served by Strata — no API cost, no Go allowance). The default delegation target — always available without user invocation; the primary agent assigns work per task — no fixed task mapping.
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
