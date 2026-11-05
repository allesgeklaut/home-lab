# Session model

An OpenCode V2 server plugin with two tools: `get_session_model` reads an agent's
own session selection; `switch_model` changes its model and optional reasoning
variant. By default only the `alloy` agent may switch models.

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
        "models": ["github-copilot/*"]
      }
    }
  ]
}
```

| Option | Default | Meaning |
| --- | --- | --- |
| `agents` | `["alloy"]` | Agent IDs that may call the tool |
| `models` | any available model | `provider/model` patterns the tool may select, `*` is a wildcard. A value that is not an array of strings makes every switch fail |

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
