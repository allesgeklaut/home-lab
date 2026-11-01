---
description: Tier 3 (deep reasoning frontier) worker running on GLM-5.3 (high effort; max is reserved for the orchestrator). Tasks needing more planning or investigation; the orchestrator assigns work per task — no fixed task mapping.
mode: subagent
model: mistral/zai-glm-5-3#high
permissions:
  - action: subagent
    resource: "*"
    effect: deny
---
