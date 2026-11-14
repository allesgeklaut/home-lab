import { Show, createMemo } from "solid-js"
import type { Accessor } from "solid-js"
import { usePlugin } from "@opencode/plugin/tui"
import { toneFor } from "../shared/design.js"
import { formatCost, formatPercent, formatTokens } from "../shared/format.js"
import { flattenSubagents } from "../shared/sessions.js"
import type { DashboardModel } from "../shared/types.js"
import { colorsFor, toneColor } from "./theme.js"
import { useDashboardModel } from "./use-model.js"

/** One-line telemetry strip for the sidebar footer. */
export function CompactFooter(props: { sessionID: string }) {
  const context = usePlugin()
  const colors = createMemo(() => colorsFor())
  const state = useDashboardModel(context, () => props.sessionID)
  const model = state.model

  return (
    <Show
      when={model()}
      fallback={
        <box flexDirection="row" width="100%">
          <text fg={colors().faint}>SESSION —</text>
        </box>
      }
    >
      {(dashboard: Accessor<DashboardModel>) => (
        <box flexDirection="row" gap={1} width="100%">
          <text fg={toneColor(colors(), toneFor(dashboard().context.percent))}>
            {formatPercent(dashboard().context.percent, 0)}
          </text>
          <text fg={colors().faint}>
            {formatTokens(dashboard().context.used)}/{formatTokens(dashboard().context.limit)}
          </text>
          <text fg={colors().muted}>{formatCost(dashboard().totalCost)}</text>
          <Show when={flattenSubagents(dashboard().subagents).length > 0}>
            <text
              fg={
                dashboard().runningSubagents > 0 ? colors().accent : colors().faint
              }
            >
              {dashboard().runningSubagents > 0 ? "●" : "○"}
              {dashboard().runningSubagents}/{flattenSubagents(dashboard().subagents).length}
            </text>
          </Show>
          <Show when={dashboard().status === "running"}>
            <text fg={colors().accent}>LIVE</text>
          </Show>
        </box>
      )}
    </Show>
  )
}
