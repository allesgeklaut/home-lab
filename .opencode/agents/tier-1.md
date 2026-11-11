---
description: Tier 1 general-purpose worker on DeepSeek V4.1 Flash (max effort). Default worker and model-recommendation consultant; the primary agent assigns work per task — no fixed task mapping.
mode: subagent
model: mistral/deepseek-v4-1-flash-1017#max
permissions:
  - action: subagent
    resource: "*"
    effect: deny
  - action: question
    resource: "*"
    effect: deny
---
