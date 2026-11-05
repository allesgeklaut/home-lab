---
description: Tier 2 (standard) general-purpose worker on Mistral Large 4 (high effort). Everyday implementation; the primary agent assigns work per task — no fixed task mapping.
mode: subagent
model: mistral/mistral-large-4#high
permissions:
  - action: subagent
    resource: "*"
    effect: deny
  - action: question
    resource: "*"
    effect: deny
---
