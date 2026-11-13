# Session cost

Read-only OpenCode server plugin exposing `get_session_cost`. The tool reports the
calling session's OpenCode-reported cost and the summed cost of its immediate
child sessions separately, plus their combined total when cost data is complete.
It does not include deeper descendants in the child figure or combined total.

Enabled in `/opt/stacks/opencode.json`, which is also the symlinked global
OpenCode configuration at `~/.config/opencode/opencode.json`:

```jsonc
"plugins": ["/opt/stacks/.opencode/session-cost-plugin"]
```

Ask an agent, for example: “Check the session cost before delegating more work;
keep this task near $1 if practical.” This is advisory, not a spending cap.
Reported costs may lag in-flight work, are read at slightly different times and
are not necessarily provider billing.

Run the tests with `node --test` in this directory.
