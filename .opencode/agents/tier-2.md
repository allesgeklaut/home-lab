---
description: Tier 2 (standard) general-purpose worker on Kimi K2.7 Code. Everyday implementation; the primary agent assigns work per task — no fixed task mapping.
mode: subagent
model: opencode-go/kimi-k2.7-code
permissions:
  - action: subagent
    resource: "*"
    effect: deny
  - action: question
    resource: "*"
    effect: deny
---
