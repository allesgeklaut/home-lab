---
description: Tier 4 (expert) general-purpose worker on Kimi K3.1 Pro (max effort). Justified high-capability assignments or escalation; the primary agent assigns work per task — no fixed task mapping.
mode: subagent
model: opencode-go/kimi-k3.1-pro#max
permissions:
  - action: subagent
    resource: "*"
    effect: deny
  - action: question
    resource: "*"
    effect: deny
---
