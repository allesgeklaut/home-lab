import { For, Show, createEffect, createMemo, createSignal } from "solid-js"
import type { Accessor } from "solid-js"
import type { BoxRenderable } from "@opentui/core"
import { TextAttributes } from "@opentui/core"
import { usePlugin } from "@opencode/plugin/tui"
import type { PanelInput } from "@opencode/plugin/tui/context"
import { toneFor } from "../shared/design.js"
import {
  formatAgo,
  formatCost,
  formatDuration,
  formatModel,
  formatPercent,
  formatSpeed,
  formatTokens,
} from "../shared/format.js"
import { visibleSubagents } from "../shared/sessions.js"
import type { DashboardModel, DashboardOptions } from "../shared/types.js"
import { requestCompaction } from "./compact-action.js"
import { AgentRow, Bar, Rule, SectionTitle, Sparkline, Stat } from "./components.js"
import { openSession } from "./navigation.js"
import { colorsFor, toneColor } from "./theme.js"
import { useDashboardModel } from "./use-model.js"

export const PANEL_NAME = "dashboard"

export function DashboardPanel(props: { panel: PanelInput }) {
  const context = usePlugin()
  const [selected, setSelected] = createSignal(0)
  const [compacting, setCompacting] = createSignal(false)

  const options = () => (context.options ?? {}) as DashboardOptions
  const colors = createMemo(() => colorsFor())
  const state = useDashboardModel(context, () => props.panel.sessionID)
  const model = state.model
  const now = state.now
  const lastUpdate = state.lastUpdate
  const refresh = state.refresh
  const agents = createMemo(() =>
    visibleSubagents(model()?.subagents ?? [], options().maxSubagents ?? 8),
  )
  const narrow = () => props.panel.width < 46

  createEffect(() => {
    const count = agents().visible.length
    if (selected() >= count) setSelected(Math.max(0, count - 1))
  })

  const compact = () => {
    if (compacting()) return
    requestCompaction(context, props.panel.sessionID, setCompacting)
  }

  let panelRef: BoxRenderable | undefined
  context.keymap.layer(() => ({
    target: () => panelRef,
    commands: [
      {
        id: "session-dashboard.refresh",
        title: "Refresh session dashboard",
        bind: "r",
        run: refresh,
      },
      {
        id: "session-dashboard.compact",
        title: "Compact session now",
        description: "Request OpenCode session compaction",
        group: "Session",
        bind: "c",
        enabled: () => options().tui?.compact !== false && !compacting(),
        run: () => {
          compact()
        },
      },
      {
        id: "session-dashboard.fullscreen",
        title: "Toggle dashboard fullscreen",
        bind: "f",
        run: () => props.panel.toggleFullscreen(),
      },
      {
        id: "session-dashboard.next",
        title: "Next subagent",
        bind: "j",
        run: () =>
          setSelected((index) =>
            Math.min(index + 1, Math.max(0, agents().visible.length - 1)),
          ),
      },
      {
        id: "session-dashboard.previous",
        title: "Previous subagent",
        bind: "k",
        run: () => setSelected((index) => Math.max(0, index - 1)),
      },
      {
        id: "session-dashboard.open",
        title: "Open subagent session",
        bind: "return",
        run: () => {
          const node = agents().visible[selected()]
          if (node) openSession(context, node.session.id)
        },
      },
    ],
  }))

  return (
    <box
      ref={(element) => (panelRef = element)}
      focusable
      focused={props.panel.focused}
      flexDirection="column"
      width="100%"
      height="100%"
      padding={1}
      gap={0}
      backgroundColor={colors().panel}
    >
      <box flexDirection="row" justifyContent="space-between" width="100%">
        <text fg={colors().accent} attributes={TextAttributes.BOLD}>
          SESSION // TELEMETRY
        </text>
        <text fg={model()?.status === "running" ? colors().accent : colors().faint}>
          {model()?.status === "running" ? "● LIVE" : "○ IDLE"}
        </text>
      </box>
      <Rule colors={colors()} />

      <scrollbox flexGrow={1} width="100%" scrollbarOptions={{ visible: false }}>
        <Show
          when={model()}
          fallback={
            <box paddingTop={1}>
              <text fg={colors().muted}>Waiting for session data…</text>
            </box>
          }
        >
          {(dashboard: Accessor<DashboardModel>) => (
            <box flexDirection="column" width="100%" gap={0}>
            <box flexDirection="column" paddingTop={1} paddingBottom={1} gap={0}>
              <box flexDirection="row" justifyContent="space-between" width="100%" height={1}>
                <text fg={colors().faint}>MODEL</text>
                <text fg={colors().faint}>PHOSPHOR</text>
              </box>
              <text fg={colors().text} height={1} wrapMode="none" truncate>
                {formatModel(dashboard().session.model, dashboard().model)}
              </text>
              <text
                fg={
                  dashboard().status === "running" ? colors().accent : colors().faint
                }
                height={1}
                wrapMode="none"
                truncate
              >
                {dashboard().status === "running" ? "● RUNNING" : "○ IDLE"} ·{" "}
                {compactionLabel(dashboard().context.compaction)}
                {dashboard().workingSince
                  ? ` · ${formatDuration(now() - dashboard().workingSince!)}`
                  : ""}
              </text>
            </box>

            <Rule colors={colors()} />
            <box flexDirection="column" paddingTop={1} paddingBottom={1} gap={0}>
              <SectionTitle
                index="01"
                title="Context"
                right="Configured budget"
                colors={colors()}
              />
              <box
                flexDirection="row"
                justifyContent="space-between"
                width="100%"
                height={1}
              >
                <text
                  fg={toneColor(colors(), toneFor(dashboard().context.percent))}
                  attributes={TextAttributes.BOLD}
                >
                  {formatPercent(dashboard().context.percent, 0)}
                </text>
                <text fg={colors().muted}>
                  {formatTokens(dashboard().context.used)} /{" "}
                  {formatTokens(dashboard().context.limit)}
                </text>
              </box>
              <Bar ratio={dashboard().context.percent} colors={colors()} />
              <box flexDirection="row" justifyContent="space-between" width="100%" height={1}>
                <text fg={colors().faint}>
                  COMPACT AT{" "}
                  {Number.isFinite(dashboard().context.ceiling)
                    ? formatTokens(dashboard().context.ceiling)
                    : "—"}
                </text>
                <text fg={colors().faint}>
                  {formatPercent(dashboard().context.ceilingPercent, 0)} ·{" "}
                  {dashboard().context.remaining !== undefined
                    ? `${formatTokens(dashboard().context.remaining!)} HEADROOM`
                    : "— HEADROOM"}
                </text>
              </box>
              <Sparkline
                values={dashboard().history}
                compactions={dashboard().historyCompactions}
                colors={colors()}
                maxWidth={Math.max(8, props.panel.width - 6)}
              />
              <box
                flexDirection="row"
                justifyContent="space-between"
                width="100%"
                height={1}
              >
                <box flexGrow={1} height={1} minWidth={0}>
                  <Show when={dashboard().compactions.count > 0}>
                    <text fg={colors().faint} height={1} wrapMode="none" truncate>
                      {dashboard().compactions.count} COMPACTIONS
                      {dashboard().compactions.lastAt
                        ? ` · LAST ${formatAgo(
                            now() - dashboard().compactions.lastAt!,
                          ).toUpperCase()}`
                        : ""}
                    </text>
                  </Show>
                </box>
                <Show when={options().tui?.compact !== false}>
                  <box
                    height={1}
                    flexShrink={0}
                    onMouseDown={(event) => {
                      if (event.button !== 0) return
                      compact()
                    }}
                  >
                    <text
                      selectable={false}
                      fg={colors().accent}
                      height={1}
                      wrapMode="none"
                    >
                      {compacting() ? "[C] COMPACTING…" : "[C] COMPACT"}
                    </text>
                  </box>
                </Show>
              </box>
            </box>

            <Rule colors={colors()} />
            <box flexDirection="column" paddingTop={1} paddingBottom={1} gap={0}>
              <SectionTitle
                index="02"
                title="Tokens"
                right="Session cumulative"
                colors={colors()}
              />
              <Show
                when={!narrow()}
                fallback={
                  <box flexDirection="column" width="100%" gap={0}>
                    <Stat
                      label="Input"
                      value={formatTokens(dashboard().usage.input)}
                      colors={colors()}
                    />
                    <Stat
                      label="Output"
                      value={formatTokens(dashboard().usage.output)}
                      colors={colors()}
                    />
                    <Stat
                      label="Cache R"
                      value={formatTokens(dashboard().usage.cacheRead)}
                      colors={colors()}
                    />
                    <Stat
                      label="Cache W"
                      value={formatTokens(dashboard().usage.cacheWrite)}
                      colors={colors()}
                    />
                    <Stat
                      label="Total"
                      value={formatTokens(dashboard().usage.total)}
                      colors={colors()}
                      accent
                    />
                    <Stat
                      label="Hit rate"
                      value={formatPercent(dashboard().usage.cacheHitRate, 0)}
                      colors={colors()}
                      accent
                    />
                    <Stat
                      label="Gen t/s"
                      value={formatSpeed(dashboard().speed.generation)}
                      colors={colors()}
                    />
                    <Stat
                      label="Prefill t/s"
                      value={formatSpeed(dashboard().speed.prefill)}
                      colors={colors()}
                    />
                  </box>
                }
              >
                <box flexDirection="row" gap={2} width="100%">
                  <box flexDirection="column" flexGrow={1} gap={0}>
                    <Stat
                      label="Input"
                      value={formatTokens(dashboard().usage.input)}
                      colors={colors()}
                    />
                    <Stat
                      label="Cache R"
                      value={formatTokens(dashboard().usage.cacheRead)}
                      colors={colors()}
                    />
                    <Stat
                      label="Total"
                      value={formatTokens(dashboard().usage.total)}
                      colors={colors()}
                      accent
                    />
                    <Stat
                      label="Gen t/s"
                      value={formatSpeed(dashboard().speed.generation)}
                      colors={colors()}
                    />
                  </box>
                  <box flexDirection="column" flexGrow={1} gap={0}>
                    <Stat
                      label="Output"
                      value={formatTokens(dashboard().usage.output)}
                      colors={colors()}
                    />
                    <Stat
                      label="Cache W"
                      value={formatTokens(dashboard().usage.cacheWrite)}
                      colors={colors()}
                    />
                    <Stat
                      label="Hit rate"
                      value={formatPercent(dashboard().usage.cacheHitRate, 0)}
                      colors={colors()}
                      accent
                    />
                    <Stat
                      label="Prefill t/s"
                      value={formatSpeed(dashboard().speed.prefill)}
                      colors={colors()}
                    />
                  </box>
                </box>
              </Show>
              <box
                flexDirection="row"
                justifyContent="space-between"
                width="100%"
                paddingTop={1}
              >
                <text fg={colors().faint}>COST</text>
                <text fg={colors().text}>{formatCost(dashboard().totalCost)}</text>
                <text fg={colors().faint}>
                  OWN {formatCost(dashboard().usage.cost)} · SUBAGENTS{" "}
                  {formatCost(dashboard().subagentCost)}
                </text>
              </box>
            </box>

            <Rule colors={colors()} />
            <box flexDirection="column" paddingTop={1} paddingBottom={1} gap={0}>
              <SectionTitle
                index="03"
                title="Subagents"
                right={
                  agents().total > 0
                    ? `${agents().running} RUNNING · ${agents().total} TOTAL`
                    : undefined
                }
                colors={colors()}
              />
              <Show
                when={agents().visible.length > 0}
                fallback={<text fg={colors().faint}>NO SUBAGENTS</text>}
              >
                <box flexDirection="column" gap={1} width="100%">
                  <For each={agents().visible}>
                    {(node, index) => (
                      <AgentRow
                        node={node}
                        selected={index() === selected()}
                        colors={colors()}
                        now={now()}
                        onOpen={() => openSession(context, node.session.id)}
                      />
                    )}
                  </For>
                  <Show when={agents().hidden > 0}>
                    <text fg={colors().faint} height={1}>
                      +{agents().hidden} MORE · {agents().total} TOTAL
                    </text>
                  </Show>
                </box>
              </Show>
            </box>
          </box>
        )}
      </Show>
      </scrollbox>

      <Rule colors={colors()} />
      <box flexDirection="row" justifyContent="space-between" width="100%" height={1}>
        <text fg={colors().faint}>
          UPDATED {formatAgo(now() - lastUpdate())}
        </text>
        <box flexDirection="row" gap={0} height={1}>
          <text fg={colors().faint} height={1} wrapMode="none" truncate>
            [R] REFRESH  [F] FULL
          </text>
          <Show when={agents().visible.length > 0}>
            <text fg={colors().faint} height={1} wrapMode="none" truncate>
              {"  [J/K] SELECT  [ENTER] OPEN"}
            </text>
          </Show>
        </box>
      </box>
    </box>
  )
}

function compactionLabel(kind: "summary" | "native" | "off"): string {
  if (kind === "native") return "NATIVE COMPACTION"
  if (kind === "off") return "AUTO-COMPACT OFF"
  return "SUMMARY COMPACTION"
}
