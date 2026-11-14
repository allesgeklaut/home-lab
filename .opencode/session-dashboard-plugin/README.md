# opencode-session-dashboard

A telemetry panel for OpenCode sessions: the model in use, the configured
context budget, token usage, throughput, cache hit rate, cost, compaction
history, and the subagent tree — in the terminal and in the desktop app's side
panel. It only reads session data, with one deliberate exception: the explicit
**Compact** action asks OpenCode to compact the current session.

The look is a minimal retro-futuristic instrument panel: phosphor green on a
dark grid, monospace figures, segmented bars, a context sparkline, and thin
rules. The green matrix styling is fixed — there is no appearance toggle.

## What it shows

- **Model** — provider/model plus the active variant, e.g.
  `mistral/zai-glm-5-3#max`.
- **Context** — used tokens against the *configured* model budget, not the raw
  model window, plus the auto-compaction ceiling, remaining headroom, and a
  sparkline of the session's assistant steps. The line starts at zero and
  resets to zero at each completed compaction, so the fill before and after
  every compaction is visible. Compaction points are marked, and the panel
  shows how many compactions have run and how long ago the last one ran.
- **Tokens** — input, output, cache read, cache write, cumulative total, cache
  hit rate, average generation throughput, and — when the host reports
  first-token timing — average prefill throughput.
- **Cost** — this session, its subagents, and the combined total.
- **Subagents** — running/idle state, agent name and title, model, elapsed
  time, tokens, cost, and per-subagent context usage. Nested subagents are
  indented under their parent with tree connectors. Running agents come
  first; the list is capped at the most recent eight by default, with a
  `+N more` count. Click a row in the GUI, or select it with `j`/`k` and press
  `enter` in the TUI, to open that subagent's session.

The plugin only reads session data and keeps no state of its own. The one
write action is the explicit **Compact** control, which calls OpenCode's
`session.compact` for the current session; nothing compacts automatically
unless you press it.

## Install

Add the package to the `plugins` array in `opencode.json`:

```json
{
  "plugins": [
    {
      "package": "/opt/stacks/.opencode/session-dashboard-plugin",
      "options": {
        "tui": { "panel": true, "footer": true },
        "gui": { "panel": true, "composerPill": true }
      }
    }
  ]
}
```

Then check that the host sees it:

```sh
opencode plugin list
```

The package exposes three entrypoints:

| Export | Purpose |
| --- | --- |
| `.` | No-op server part; keeps the plugin id stable (`session-dashboard`). |
| `./tui` | Terminal UI panel, sidebar footer, and `/dashboard` command. |
| `./gui-experimental` | Desktop side panel, command, menu item, and composer pill. Prebuilt CommonJS in `dist/`. |

The TUI and GUI are independent. If the experimental GUI API changes, the TUI
keeps working; the GUI setup is guarded at runtime and simply backs off.

## TUI

- `/dashboard` (alias `/dash`) opens the panel.
- `ctrl+shift+d` opens it from anywhere.
- The command palette entry is **Open session dashboard**.
- The sidebar footer shows context %, used/limit, total cost, running subagent
  count, and a `LIVE` marker.

Panel keys while focused:

| Key | Action |
| --- | --- |
| `r` | Refresh session data |
| `c` | Request session compaction (or click `[C] COMPACT` in the footer) |
| `f` | Toggle fullscreen |
| `j` / `k` | Move the subagent selection |
| `enter` | Open the selected subagent session |

The panel scrolls when the terminal is short; the footer stays fixed.

## Desktop GUI

- The **Dashboard** tab appears in the session's side-panel strip.
- The **+** menu before the tabs has a **Session dashboard** item.
- The command palette has **Open session dashboard**
  (`mod+shift+d`, `/dashboard`).
- A composer pill shows context % and used tokens; clicking it opens the
  panel. The pill turns amber at 75% and red at 90%.
- The panel footer has a **Compact** button that requests session compaction;
  it disables itself while the request is in flight.
- The panel always uses the green phosphor matrix styling.

The GUI bundle is built ahead of time. Rebuild it after changing GUI code:

```sh
npm run build
```

## Options

All options are optional and live under the plugin's `options` object.

| Option | Type | Default | Meaning |
| --- | --- | --- | --- |
| `refreshMs` | number | `1000` | Live-refresh interval while the session or a subagent is running. |
| `maxSubagentDepth` | number | `2` | How many levels of nested subagents to include. |
| `maxSubagents` | number | `8` | How many subagent rows to show. Running agents are kept first; `0` shows all. |
| `contextLimit` | `"input-window"` \| `"context"` \| number | `"input-window"` | Which model limit is the context denominator, or a fixed token override. |
| `compactionBuffer` | number \| string | auto | Tokens reserved below the window before compaction. A string like `"15%"` is a percentage. |
| `showSubagentContext` | boolean | `true` | Show per-subagent context bars. |
| `tui.panel` | boolean | `true` | Register the TUI session panel. |
| `tui.footer` | boolean | `true` | Register the TUI sidebar footer. |
| `tui.compact` | boolean | `true` | Enable the `c` compact action in the TUI panel. |
| `gui.panel` | boolean | `true` | Register the GUI side panel, command, and menu item. |
| `gui.composerPill` | boolean | `true` | Register the GUI composer pill. |
| `gui.compact` | boolean | `true` | Show the Compact button in the GUI panel. |

The panel styling is fixed; there is no theme option.

## Context math

The denominator is the configured model budget, not the model's maximum
context window. This mirrors OpenCode's compaction logic:

1. `inputWindow = min(limit.input, limit.context)` when both are present,
   otherwise `limit.input || limit.context`.
2. The auto-compaction ceiling subtracts a reserve from that window. The
   default reserve is 15% of the window, with a minimum of 16k tokens; a
   `compactionBuffer` option overrides it.
3. Context used is the last assistant step after the last completed
   compaction and before the revert boundary:
   `input + cache.read + cache.write`.
4. The sparkline starts at zero at the session start and resets to zero at
   each completed compaction, so it shows the fill before and after every
   compaction. The TUI trims the line to the panel width; the GUI shows the
   whole session.
5. Generation throughput is
   `(output + reasoning) / (streamed - (firstToken ?? created))`, summed over
   the session. Prefill throughput is
   `(input + cache.read + cache.write) / (firstToken - created)` and is only
   shown when the host reports `firstToken`.
6. Cache hit rate is
   `cache.read / (input + cache.read + cache.write)`.

If a model reports no usable limit, the panel shows `—` rather than inventing
a number. `contextLimit` can force a fixed budget when a provider's metadata
is wrong.

## Compatibility

- `@opencode/plugin` is pinned to `2.3.0`; the package declares
  `"opencode": { "testedWith": ">=2.3.0 <2.4.0" }`.
- Both entrypoints check the host version at setup. On an untested version
  they show a warning and continue in a guarded mode; the GUI checks the
  registry and session APIs it needs before registering anything.
- The GUI part is experimental by OpenCode's own naming
  (`./gui-experimental`). A GUI API break cannot disable the TUI.
- `effect`, `solid-js`, `@opentui/*`, and `@opencode/plugin` are peer
  dependencies and are externalized from the GUI bundle, so the plugin shares
  the host's runtime instances.

## Development

```sh
npm install
npm run typecheck   # GUI and TUI TypeScript projects
npm test            # unit tests for usage, context, speed, formatting, sessions
npm run build       # dist/gui-experimental.cjs
npm run dev         # rebuild the GUI bundle on change
```

The shared core lives in `src/shared/`; the TUI in `src/tui/`; the GUI in
`src/gui/`. The static design mockup is in `mockup/`.

## License

Private / local use.
