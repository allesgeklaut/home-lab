---
description: Tier 3 (deep reasoning) general-purpose worker on GLM-5.3 (high effort). Elevated uncertainty or escalation; the primary agent assigns work per task — no fixed task mapping.
mode: subagent
model: opencode-go/glm-5.3#high
permissions:
  - action: subagent
    resource: "*"
    effect: deny
  - action: question
    resource: "*"
    effect: deny
---
