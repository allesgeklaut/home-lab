# Session model

An OpenCode V2 server plugin with optional Auto-gated prompt routing and two
tools: `get_session_model` reads an agent's own session selection;
`switch_model` changes its model and optional reasoning variant. Automatic
routing applies only when the session's current model is the `mistral/auto`
alias; a concrete model choice is never rerouted. The prompt hook only picks a
target. Auto's fallback model then calls `switch_model` inside the session,
before doing the task. No OpenCode source or TUI changes are required.

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
        "agents": ["alloy", "build", "plan"],
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
| `agents` | `["*"]` | Agent ID patterns allowed to switch manually. Reviewer is always routed-only. A validated Auto handoff is allowed independently of this list; normal agent tool permissions still apply |
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
  first admitted prompt, the hook assesses the task and records a concrete
  target from the shortlist, without switching.
- A concrete manual choice is never rerouted. Selecting Auto in an existing
  session routes its next admitted prompt.
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

After admission, the `context` hook appends a transient user-role reminder with
the exact target and a `routing` token. It identifies the current epoch and
explicitly excludes earlier completed switches. User text and attachment offsets remain
unchanged. The fallback must call `switch_model` alone before task work; the
next model request uses the selected concrete model/variant. This adds a model
turn and depends on the fallback following the instruction. It is not a fix for
OpenCode's separate stale TUI-selection cache defect.

The tool validates the token against plugin-owned storage, the latest admitted
user message, active agent, current Auto selection/epoch, shortlist, model policy
and availability. Stale, forged, replayed or changed-target handoffs are
rejected. A failed authorized attempt is not repeatedly instructed. Changes and
switching cannot be atomic because the server exposes no compare-and-switch
API; a manual selection in the final check/dispatch gap can still race.

Durable bookkeeping uses `routing/v2/<sessionID>/<epoch>`. A fresh session has
only agent/model/location selection events; otherwise a surviving explicit Auto
selection event is required. Compaction without that event skips routing.
Concurrent admissions serialize and reassess the epoch. A pending marker is
saved before paid selection, preventing repeated bills across reloads or failed
admissions. Existing v2 markers from the older direct-switch implementation
remain terminal and are not replayed.

All admission waits share `routing.timeoutMs`. Late selector/read results cannot
publish a handoff. SDK 2.2.6 cannot cancel dispatched provider work, so generation
may continue consuming tokens after timeout. Instruction lookup has its own
two-second read budget and fails open. No hook calls `switchModel`.

Prompt metadata reports `picked` (not switched) with model/reason/epoch, or
`fallback`. The durable marker additionally records `dispatching`, `switched`,
or `failed` for a tool attempt; a dispatched switch with a lost response may
still apply. Early skips add no routing metadata. If switching or routing fails,
continue on the current selection and report the constraint, without selecting
another target or retrying automatically. To retry Auto, choose concrete then
Auto again. Use `/reload` if an already-loaded location retains old code/options;
no service restart is needed.

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
tool surface. It allows `switch_model` only for the plugin-validated Auto
handoff; manual reviewer switching is rejected even with `agents: ["*"]`.
The review skill launches it without a model override, so the same prompt hook
picks through `routing.selector` and `routing.candidates`, followed by the
fallback's in-session switch. There is no second selector list or per-call model
override. The reviewer has no shell or git access, so the review skill supplies
exact references and a sanitized diff; the reviewer can still inspect current
files with read/glob/grep. After the handoff it reports `get_session_model` output so the parent
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
- Manual callers outside `agents` get an error. This installation lists only
  the primary agents (alloy, build, plan), so tier workers keep their pinned
  models and cannot switch manually. The named reviewer always needs
  a valid `routing` token. Other tool-denying agent permissions remain in force.

## Test

```powershell
npm test
```

The repository config test validates the Auto alias wiring, routing options,
reviewer permissions, and tier pins. It does not exercise live routing or
provider calls.
