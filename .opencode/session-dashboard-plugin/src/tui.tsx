import { Plugin } from "@opencode/plugin/tui"
import { isTestedVersion, missingFunctions, TESTED_WITH } from "./shared/compat.js"
import type { DashboardOptions } from "./shared/types.js"
import { CompactFooter } from "./tui/compact.js"
import { DashboardPanel } from "./tui/dashboard.js"

const PANEL_NAME = "dashboard"

export default Plugin.define({
  id: "session-dashboard",
  setup(context) {
    const options = (context.options ?? {}) as DashboardOptions
    const missing = [
      ...missingFunctions(context.ui, ["slot"]),
      ...missingFunctions(context.ui?.panel, ["open"]),
      ...missingFunctions(context.ui?.router, ["navigate"]),
      ...missingFunctions(context.keymap, ["layer"]),
      ...missingFunctions(context.data, ["listen"]),
      ...missingFunctions(context.data?.session, ["get", "status"]),
      ...missingFunctions(context.data?.session?.message, ["list"]),
      ...missingFunctions(context.data?.location?.model, ["list"]),
    ]

    if (missing.length > 0) {
      context.ui?.toast?.show?.({
        title: "Session dashboard",
        message: `TUI API mismatch (${missing.join(", ")}); dashboard disabled.`,
        variant: "error",
      })
      return
    }

    if (!isTestedVersion(context.app?.version)) {
      context.ui.toast.show({
        title: "Session dashboard",
        message: `OpenCode ${context.app?.version || "unknown"} is outside the tested range ${TESTED_WITH}; some panel data may be missing.`,
        variant: "warning",
        duration: 8000,
      })
    }

    const disposers: Array<() => void> = []

    if (options.tui?.panel !== false) {
      disposers.push(
        context.ui.slot({
          append: "session.panel",
          render: (input) =>
            input.name === PANEL_NAME ? <DashboardPanel panel={input} /> : null,
        }),
      )
    }

    if (options.tui?.footer !== false) {
      disposers.push(
        context.ui.slot({
          append: "sidebar.footer",
          render: (input) => <CompactFooter sessionID={input.sessionID} />,
        }),
      )
    }

    if (options.tui?.panel !== false) {
      context.keymap.layer(() => ({
        mode: "global",
        commands: [
          {
            id: "session-dashboard.open",
            title: "Open session dashboard",
            description: "Model, context budget, tokens, cost, and subagents",
            group: "Session",
            bind: "ctrl+shift+d",
            palette: true,
            slash: { name: "dashboard", aliases: ["dash"] },
            run: () => {
              context.ui.panel.open(PANEL_NAME)
            },
          },
        ],
      }))
    }

    return () => {
      for (const dispose of disposers) dispose()
    }
  },
})
