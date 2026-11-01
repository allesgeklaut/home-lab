---
description: Tier 2 (standard frontier) worker running on Mistral Large 4 (high effort). Everyday implementation; the orchestrator assigns work per task — no fixed task mapping.
mode: subagent
model: mistral/mistral-large-4#high
permissions:
  - action: subagent
    resource: "*"
    effect: deny
---
