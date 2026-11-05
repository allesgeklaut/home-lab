---
description: Tier 4 (expert) general-purpose worker on GLM-5.3 (max effort). Justified high-capability assignments or escalation; the primary agent assigns work per task — no fixed task mapping.
mode: subagent
model: mistral/zai-glm-5-3#max
permissions:
  - action: subagent
    resource: "*"
    effect: deny
  - action: question
    resource: "*"
    effect: deny
---
