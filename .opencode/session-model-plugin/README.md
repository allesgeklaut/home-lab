# Session model

An OpenCode V2 server plugin with optional automatic first-prompt model routing
and two tools: `get_session_model` reads an agent's own session selection;
`switch_model` changes its model and optional reasoning variant. By default only
`alloy` may use the switching **tool**. Automatic routing is independent of that
agent restriction and applies to every new main session.

This is an OpenCode plugin, not a Copilot CLI plugin, so it is not in the
marketplace manifest.

## Install

Add the plugin directory to `plugins` in `~/.config/opencode/opencode.jsonc`,
preserving other entries:

```jsonc
{
  "plugins": ["<path-to>/session-model-plugin"]
}
```

OpenCode loads it without a restart. Use `/reload` to force a reload if needed.

## Options

Pass options with the object form:

```jsonc
{
  "plugins": [
    {
      "package": "<path-to>/session-model-plugin",
      "options": {
        "agents": ["alloy"],
        "models": ["mistral/*"],
        "routing": {
          "selector": "mistral/mistral-large-4#medium",
          "timeoutMs": 20000,
          "candidates": [
            {
              "model": "mistral/zai-glm-5-3-flash#high",
              "description": "Fast, low-cost general coding and analysis with high reasoning."
            },
            {
              "model": "mistral/zai-glm-5-3#max",
              "description": "Stronger reasoning for difficult or high-consequence tasks."
            }
          ]
        }
      }
    }
  ]
}
```

| Option | Default | Meaning |
| --- | --- | --- |
| `agents` | `["alloy"]` | Agent IDs that may call the switching tool; does not restrict automatic routing |
| `models` | any available model | `provider/model` patterns both routing and the tool may select, `*` is a wildcard. Invalid values fail closed |
| `routing` | disabled | Enable automatic routing by supplying the object above; omit it or set `routing.enabled: false` to disable |
| `routing.selector` | required | Exact model/variant used for the stateless task assessment |
| `routing.candidates` | required | Fixed shortlist of `{model, description}` entries with exact model/variant references |
| `routing.timeoutMs` | `20000` | Total admission-routing wait budget (all API/storage calls included), from 1 to 120000 milliseconds |

## Automatic routing

- A `prompt` admission hook runs the assessment **before the first answer**, for
  any selected main agent (Build, Plan, Alloy, or a custom agent). It changes only
  the model, never the agent or its permissions.
- Child/subagent sessions, forks and sessions with existing conversation history
  are left untouched. Follow-ups, agent changes, compaction and reloads do not
  trigger reassessment.
- The selector is one stateless `experimental.generate.text` request, with no
  tools or child session. It sees only the first 24000 characters of the task and
  whether attachments exist, not their contents, project files or conversation
  history. Attachment-heavy tasks may therefore need a manual model choice.
  This excerpt is transmitted to the selector model's provider (Mistral in this
  installation), even when the main agent uses another provider or a local model.
- Candidates and their reasoning variants live in configuration, not the agent
  prompt. Availability, variants and the `models` policy are checked internally;
  unavailable candidates are filtered before assessment. There are no
  model-listing tool calls in the conversation.
- A durable per-session pending marker is saved before generation; concurrent
  admissions share one attempt. Errors, invalid replies and timeouts **before a
  switch is dispatched** retain the current model. A saved marker prevents retry,
  even after an interrupted attempt or failed prompt admission. Failures before
  the marker is saved may be retried, but never start untracked generation.
- Every API/storage wait shares the same timeout budget. Late selector/read
  results cannot start a switch. SDK 2.2.6's Promise adapter does not support
  cancelling those calls: provider generation may continue consuming tokens after
  fallback. If a switch was dispatched but its completion was not confirmed when
  waiting failed or timed out, its outcome is reported as `uncertain` and it may
  still apply later. A confirmed switch remains `selected` if only saving its
  result fails or times out.
- Manual model/agent changes completed before the final session check are
  respected. That check and the switch are not atomic, so a manual change in
  between may still be overwritten. On a fresh session, the initial selection is
  the fallback, not an opt-out from automatic routing.
- A completed attempt records selection/reason, fallback or an uncertain switch
  in the prompt's `metadata.sessionModelRouting`. Plugin storage at
  `routing/v1/<sessionID>` also records the result when time and storage permit;
  otherwise its pending marker remains. Skipped attempts (including an existing
  pending marker) add no routing metadata. The prompt text itself is not changed.
  No service restart is needed after editing configuration. Use `/reload` if an
  already-loaded location retains older plugin options.

## Behavior

- `get_session_model` reads the calling session's model and selected variant without
  changing anything. It is available to all agents; switching restrictions do not
  restrict reading their own selection. An omitted variant is reported as default,
  not as a verified effective provider reasoning effort.
- The target is `provider/model` or `provider/model#variant`. It must be an
  available model, and the variant must exist for it.
- The switch applies to subsequent model requests of the same session.
- Switching starts with a cold prompt cache. The agent's instructions determine
  when a switch is worthwhile.
- Other agents calling `switch_model` get an error. The tool is still listed for them.

## Test

```powershell
npm test
```
