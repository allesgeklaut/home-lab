---
description: Tier 1 (fast/cost-efficient) general-purpose worker on Mistral Large 4 (medium effort). Default worker and model-recommendation consultant; the primary agent assigns work per task — no fixed task mapping.
mode: subagent
model: mistral/mistral-large-4#medium
permissions:
  - action: subagent
    resource: "*"
    effect: deny
  - action: question
    resource: "*"
    effect: deny
---
