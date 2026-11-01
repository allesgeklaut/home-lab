---
description: Tier 1 (fast/cost-efficient) worker running on Mistral Large 4 (medium effort). Well-specified, deterministic, high-volume tasks; the orchestrator assigns work per task — no fixed task mapping.
mode: subagent
model: mistral/mistral-large-4#medium
permissions:
  - action: subagent
    resource: "*"
    effect: deny
---
