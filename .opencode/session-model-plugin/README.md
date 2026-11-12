# Session model

An OpenCode V2 server plugin with optional Auto-gated prompt routing and two
tools: `get_session_model` reads an agent's own session selection;
`switch_model` changes its model and optional reasoning variant. Automatic
routing applies only when the session's current model is the `mistral/auto`
alias; a concrete model choice is never rerouted. By default only `alloy` may
use the switching **tool**; that agent restriction does not restrict automatic
routing.

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
          "timeoutMs": 45000,
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
| `routing` | disabled | Enable Auto-gated automatic routing by supplying the object above; omit it or set `routing.enabled: false` to disable |
| `routing.selector` | required | Exact model/variant used for the stateless task assessment |
| `routing.candidates` | required | Fixed shortlist of `{model, description}` entries with exact model/variant references |
| `routing.timeoutMs` | `20000` | Total admission-routing wait budget (all API/storage calls included), from 1 to 120000 milliseconds |

This installation uses a 45-second budget because review-model assessments can
exceed the 20-second default. Concrete model selections bypass this wait.

There is no separate Auto option. Auto is a model alias in the provider
configuration, and routing is gated on the session selecting it.

## Auto model setup

Configure a selectable Auto alias and make it the global default for new
sessions:

```jsonc
{
  "model": "mistral/auto",
  "providers": {
    "mistral": {
      "models": {
        "auto": {
          "modelID": "zai-glm-5-3-flash",
          "name": "Auto (routed)",
          "limit": { "input": 307200 }
        }
      }
    }
  }
}
```

- The root `model` sets the default for new sessions. V2 retains only the
  provider and model from the root selection, not a variant, so do not add a
  `#variant` suffix there.
- `auto` is the selectable reference; `modelID` is the real model sent to the
  provider. The alias inherits capabilities, limits, and reasoning variants from
  that configured model. An explicit `limit.input` preserves the 307200-token
  input budget for the alias.
- The fallback model must already be configured and active, and remain in the
  shared shortlist. It is also the safe fallback when routing fails. Auto itself
  must not appear in `routing.candidates`; the selector chooses only among
  concrete candidates.
- A session with no stored model resolves a usable agent model preference, then
  the global default. Routing runs only when that resolved model is Auto.

## Automatic routing

Routing is gated on the current session model being the Auto alias:

- New main sessions use Auto when no more specific model is stored or configured
  for the selected agent; the root `model` supplies the global default. On the
  first admitted prompt, the hook assesses the task and switches to a concrete
  model from the shortlist.
- A concrete manual choice is never rerouted. Selecting Auto in an existing
  session routes its next admitted prompt; the router then switches the session
  back to a concrete model.
- Dedicated child sessions configured with Auto, such as the `reviewer`
  subagent, use the same hook, selector, and shortlist. Regular tier workers
  keep their concrete pinned models and skip routing.
- Forks are skipped unless Auto is explicitly selected in the fork after
  creation. Follow-ups, agent changes, compaction, and reloads do not trigger
  routing unless Auto is selected again.
- Later objectives do not trigger new routing while the session remains on a
  concrete model. If routing fails, the session keeps the Auto alias and
  requests use its configured fallback model.
- A failed or same-model Auto selection does not retrigger. A same-model switch
  is a no-op; to retry, choose a concrete model and then Auto again.

Durable bookkeeping is scoped to each Auto-selection epoch (v2), not the
session lifetime, so a completed attempt for one Auto selection does not block
a later concrete-to-Auto selection. Eligibility uses a selection-only initial
history marker: a session is fresh when its history contains only
agent/model/location switch events. If compaction removes the Auto selection
event and the remaining history is nonfresh, routing is skipped rather than
inventing a new epoch; select Auto again to route.

Concurrent admissions serialize per session. Waiters re-check the current Auto
epoch after the active attempt releases, so a newly selected Auto epoch is not
lost. A durable marker is saved before generation. Errors, invalid replies, and
timeouts **before a switch is dispatched** retain the current model. A saved
marker prevents retry for that selection event, even after an interrupted
attempt or failed prompt admission. Failures before the marker is saved may be
retried, but never start untracked generation.

Every API/storage wait shares the same timeout budget. Late selector or read
results cannot start a switch. SDK 2.2.6's Promise adapter does not support
cancelling those calls: provider generation may continue consuming tokens after
fallback. If a switch was dispatched but its completion was not confirmed when
waiting failed or timed out, its outcome is reported as `uncertain` and it may
still apply later. A confirmed switch remains `selected` if only saving its
result fails or times out.

Manual model or agent changes completed before the final state/epoch check are
respected. That check and the switch are not atomic, so a manual change in
between may still be overwritten. On a fresh session, a concrete initial
selection is an opt-out and is not routed; only the Auto alias triggers routing,
with its mapped fallback serving requests until routing completes or fails.

A completed attempt records selection/reason, fallback, or an uncertain switch
in the prompt's `metadata.sessionModelRouting`. Plugin storage at
`routing/v2/<sessionID>/<epoch>` also records the result for that Auto-selection
event when time and storage permit; otherwise its pending marker remains. Early
skips, such as a concrete selection, no provable Auto epoch, or an existing
marker, add no routing metadata. The prompt text itself is not changed. No
service restart is needed after editing configuration. Use `/reload` if an
already-loaded location retains older plugin options.

The selector is one stateless `experimental.generate.text` request, with no
tools or child session. It sees only the current prompt's first 24,000
characters, whether attachments exist, and the original prompt length, not
attachment contents, project files, or prior conversation history. This excerpt
is transmitted to the selector model's provider (Mistral in this installation),
even when the main agent uses another provider or a local model. Attachment-heavy
tasks may therefore need a manual model choice. A mid-session Auto selection
followed by a vague prompt such as "continue" also gives the selector little
context, so it may choose a weaker model; restate the task or choose concretely.

## Reviewer integration

`.opencode/agents/reviewer.md` sets `model: mistral/auto` and a reduced read-only
tool surface. The review skill launches it without a model override, so the same
prompt hook routes the child session through `routing.selector` and
`routing.candidates`. There is no second selector list or per-call model
override. The reviewer has no shell or git access, so the review skill supplies
exact references and a sanitized diff; the reviewer can still inspect current
files with read/glob/grep. It reports `get_session_model` output so the parent
can state the actual selection.

## Behavior

- `get_session_model` reads the calling session's model and selected variant
  without changing anything. It is available to all agents; switching
  restrictions do not restrict reading their own selection. For an alias such
  as `mistral/auto`, it also reports the provider model metadata the alias maps
  to when the model catalog exposes it. An omitted variant is reported as default,
  not as a verified effective provider reasoning effort.
- The switch target is `provider/model` or `provider/model#variant`. It must be
  an available model, and the variant must exist for it.
- The switch applies to subsequent model requests of the same session.
- Switching starts with a cold prompt cache. The agent's instructions determine
  when a switch is worthwhile.
- Other agents calling `switch_model` get an error. The tool is still listed for
  them.

## Test

```powershell
npm test
```

The repository config test validates the Auto alias wiring, routing options,
reviewer permissions, and tier pins. It does not exercise live routing or
provider calls.
