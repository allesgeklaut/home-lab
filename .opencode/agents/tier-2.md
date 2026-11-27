---
description: Tier 2 (standard) general-purpose worker on Tencent Hy4 (high effort). Everyday implementation; the primary agent assigns work per task — no fixed task mapping.
mode: subagent
model: opencode-go/hy4#high
permissions:
  - action: subagent
    resource: "*"
    effect: deny
  - action: question
    resource: "*"
    effect: deny
---
