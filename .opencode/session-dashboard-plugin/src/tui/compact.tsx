import { Show, createMemo, createSignal, onCleanup } from "solid-js"
import type { Accessor } from "solid-js"
import { usePlugin } from "@opencode/plugin/tui"
import { toneFor } from "../shared/design.js"
import { formatCost, formatPercent, formatTokens } from "../shared/format.js"
import { flattenSubagents } from "../shared/sessions.js"
import type { DashboardModel } from "../shared/types.js"
import { buildTuiModel } from "./data.js"
import { colorsFor, toneColor } from "./theme.js"

/** One-line telemetry strip for the sidebar footer. */
export function CompactFooter(props: { sessionID: string }) {
  const context = usePlugin()
  const [tick, setTick] = createSignal(0)
  const colors = createMemo(() => colorsFor())
  const model = createMemo(() => {
    tick()
    return buildTuiModel(context, props.sessionID)
  })

  const stop = context.data.listen(({ details }) => {
    if (!details.type.startsWith("session.")) return
    if (details.type.endsWith(".delta") || details.type.endsWith(".streamed")) return
    setTick((value) => value + 1)
  })
  onCleanup(stop)

  const timer = setInterval(() => {
    const current = model()
    if (current?.status === "running" || (current?.runningSubagents ?? 0) > 0) {
      setTick((value) => value + 1)
    }
  }, 1000)
  onCleanup(() => clearInterval(timer))

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
