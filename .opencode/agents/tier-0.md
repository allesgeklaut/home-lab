---
description: Tier 0 (local) worker on the local Qwen 3.8 Flash Next model (llama.cpp via Strata — no API cost, no Go allowance). Only assign when the user explicitly asks for the local model; otherwise rely on the other tiers.
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
