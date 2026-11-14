import {
  Command,
  ComposerPill,
  MenuItem,
  Panel,
  Plugin,
  Style,
} from "@opencode/plugin/gui"
import type { PanelProps, PanelTab } from "@opencode/plugin/gui"
import { isTestedVersion, missingFunctions, TESTED_WITH } from "./shared/compat.js"
import { formatCost, formatPercent, formatTokens } from "./shared/format.js"
import type { DashboardOptions } from "./shared/types.js"
import { Dashboard } from "./gui/dashboard.js"
import { buildGuiModel } from "./gui/data.js"
import css from "./gui/styles.css?inline"

const TAB_ID = "dashboard"

export default Plugin.define({
  setup(ctx) {
    const registries = { Panel, Command, MenuItem, ComposerPill, Style }
    const missing = [
      ...Object.entries(registries)
        .filter(([, registry]) => !registry)
        .map(([name]) => name),
      ...missingFunctions(ctx, ["add"]),
      ...missingFunctions(ctx.sessions, ["current"]),
      ...missingFunctions(ctx.layout, ["open"]),
      ...missingFunctions(ctx.tabs, ["open"]),
      ...missingFunctions(ctx.appearance, ["font"]),
    ]

    if (missing.length > 0) {
      ctx.toast?.show?.({
        title: "Session dashboard",
        description: `GUI API mismatch (${missing.join(", ")}); the TUI dashboard remains available.`,
        variant: "error",
        persistent: true,
      })
      return
    }

    if (!isTestedVersion(ctx.app?.version)) {
      ctx.toast?.show?.({
        title: "Session dashboard",
        description: `OpenCode ${ctx.app?.version || "unknown"} is outside the tested range ${TESTED_WITH}; the GUI panel may be limited.`,
        variant: "default",
        duration: 8000,
      })
    }

    const options = (ctx.options ?? {}) as DashboardOptions
    const open = (session = ctx.sessions.current()) => {
      if (session) ctx.layout.open(`${ctx.id}:${TAB_ID}`, session)
    }

    ctx.add(Style, css)

    if (options.gui?.panel !== false) {
      const tab: PanelTab = {
        id: TAB_ID,
        title: "Session dashboard",
        label: () => <span class="sd-tab-label">Dashboard</span>,
        closable: "compact",
      }

      const panel: Panel = {
        id: TAB_ID,
        region: "side",
        width: 360,
        mobile: {
          title: "Dashboard",
          order: 10,
          kind: "tab",
          icon: "monitor",
        },
        list(input) {
          return input.open.includes(TAB_ID) ? [tab] : []
        },
        render(props: PanelProps) {
          return <Dashboard session={props.session} options={options} />
        },
      }

      ctx.add(Panel, panel)

      ctx.add(Command, () => ({
        id: "open",
        title: "Open session dashboard",
        description: "Model, context budget, tokens, cost, and subagents",
        group: "Session",
        section: "session",
        bind: "mod+shift+d",
        slash: { name: "dashboard" },
        run: () => open(),
      }) satisfies Command)

      ctx.add(MenuItem, () => ({
        menu: "session.panel",
        id: TAB_ID,
        title: "Session dashboard",
        icon: "monitor",
        description: "Model, context, tokens, cost",
        run: () => open(),
      }) satisfies MenuItem)
    }

    if (options.gui?.panel !== false && options.gui?.composerPill !== false) {
      ctx.add(ComposerPill, {
        id: "context",
        order: 20,
        show(input) {
          const model = buildGuiModel(input.session, options)
          if (!model) return undefined
          const percent = model.context.percent
          const tone =
            percent === undefined
              ? "success"
              : percent >= 0.9
                ? "danger"
                : percent >= 0.75
                  ? "warning"
                  : "success"

          return {
            label: `${formatPercent(percent, 0)} · ${formatTokens(model.context.used)}`,
            icon: "monitor",
            tone,
            title: `Session dashboard · ${formatCost(model.totalCost)}`,
            run: () => open(input.session),
          }
        },
      })
    }
  },
})
