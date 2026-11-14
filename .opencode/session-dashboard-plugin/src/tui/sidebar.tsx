import { For, Show, createMemo, createSignal, onCleanup, onMount } from "solid-js"
import type { Accessor } from "solid-js"
import { ScrollBoxRenderable, TextAttributes } from "@opentui/core"
import type { BoxRenderable } from "@opentui/core"
import { usePlugin } from "@opencode/plugin/tui"
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
import type { DashboardModel, DashboardOptions, SubagentNode } from "../shared/types.js"
import { requestCompaction } from "./compact-action.js"
import { Bar, Rule, Sparkline } from "./components.js"
import { openSession } from "./navigation.js"
import { colorsFor, toneColor } from "./theme.js"
import type { Colors } from "./theme.js"
import { useDashboardModel } from "./use-model.js"

const SIDEBAR_AGENTS = 3
// Host-owned fixed width of the regular session sidebar in OpenCode 2.3.x.
const HOST_SIDEBAR_WIDTH = 42

/**
 * Compact telemetry section for the regular session sidebar. Clicking the
 * header opens the full dashboard panel; the context section keeps the
 * explicit compact action.
 */
export function SidebarDashboard(props: { sessionID: string; panelName?: string }) {
  const context = usePlugin()
  const [compacting, setCompacting] = createSignal(false)
  const colors = createMemo(() => colorsFor())
  const options = () => (context.options ?? {}) as DashboardOptions
  const state = useDashboardModel(context, () => props.sessionID)
  const agents = createMemo(() =>
    visibleSubagents(state.model()?.subagents ?? [], SIDEBAR_AGENTS),
  )
  let root: BoxRenderable | undefined
  let sidebarScroll: ScrollBoxRenderable | undefined
  onMount(() => {
    // The host has no sidebar-scrollbar option. This is a best-effort
    // compatibility shim, not a supported host API: find the nearest
    // scrollbox and hide only its vertical bar. The host's sidebar box is a
    // fixed 42 columns wide; the width guard keeps a future layout change
    // from hiding the transcript scrollbar instead. Wheel scrolling is
    // handled by the scrollbox itself and is unaffected by bar visibility.
    for (let parent = root?.parent; parent; parent = parent.parent) {
      if (!(parent instanceof ScrollBoxRenderable)) continue
      if (parent.parent?.width !== HOST_SIDEBAR_WIDTH) break
      sidebarScroll = parent
      parent.verticalScrollBar.visible = false
      break
    }
  })
  onCleanup(() => {
    const scroll = sidebarScroll
    sidebarScroll = undefined
    if (!scroll || scroll.isDestroyed) return
    // Return the bar to the host's auto-visibility policy when this section
    // unmounts (for example, when navigating to a subagent session).
    scroll.verticalScrollBar.resetVisibilityControl()
  })

  const compact = () => {
    if (compacting()) return
    requestCompaction(context, props.sessionID, setCompacting)
  }

  return (
    <box ref={(element) => (root = element)} flexDirection="column" width="100%" gap={0} paddingTop={1} paddingBottom={1}>
      <box
        flexDirection="row"
        justifyContent="space-between"
        width="100%"
        height={1}
        onMouseDown={(event) => {
          if (event.button !== 0 || !props.panelName) return
          context.ui.panel.open(props.panelName)
        }}
      >
        <text
          selectable={false}
          fg={colors().accent}
          attributes={TextAttributes.BOLD}
        >
          {props.panelName ? "TELEMETRY ↗" : "TELEMETRY"}
        </text>
        <text
          selectable={false}
          fg={
            state.model()?.status === "running" ? colors().accent : colors().faint
          }
        >
          {state.model()?.status === "running" ? "● LIVE" : "○ IDLE"}
        </text>
      </box>

      <Show
        when={state.model()}
        fallback={
          <text fg={colors().muted} height={1}>
            Waiting for session data…
          </text>
        }
      >
        {(dashboard: Accessor<DashboardModel>) => (
          <box flexDirection="column" width="100%" gap={0}>
            <text fg={colors().text} height={1} wrapMode="none" truncate>
              {formatModel(dashboard().session.model, dashboard().model)}
            </text>
            <text
              fg={
                dashboard().status === "running"
                  ? colors().accent
                  : colors().faint
              }
              height={1}
              wrapMode="none"
              truncate
            >
              {dashboard().status === "running" ? "● RUNNING" : "○ IDLE"}
              {dashboard().workingSince
                ? ` · ${formatDuration(state.now() - dashboard().workingSince!)}`
                : ""}
            </text>

            <SectionBreak colors={colors()} />
            <box
              flexDirection="row"
              justifyContent="space-between"
              width="100%"
              height={1}
            >
              <text fg={colors().muted}>01 CONTEXT</text>
              <text
                fg={toneColor(colors(), toneFor(dashboard().context.percent))}
                attributes={TextAttributes.BOLD}
              >
                {formatPercent(dashboard().context.percent, 0)}
              </text>
            </box>
            <box
              flexDirection="row"
              justifyContent="space-between"
              width="100%"
              height={1}
            >
              <text fg={colors().faint}>
                {formatTokens(dashboard().context.used)} /{" "}
                {formatTokens(dashboard().context.limit)}
              </text>
            </box>
            <box width="100%" height={1}>
              <text fg={colors().faint}>
                COMPACT AT {formatPercent(dashboard().context.ceilingPercent, 0)} ·{" "}
                {dashboard().context.remaining !== undefined
                  ? `${formatTokens(dashboard().context.remaining!)} HEADROOM`
                  : "— HEADROOM"}
              </text>
            </box>
            <Bar ratio={dashboard().context.percent} colors={colors()} />
            <Show when={dashboard().history.length > 0}>
              <box width="100%" height={1} />
            </Show>
            <Sparkline
              values={dashboard().history}
              compactions={dashboard().historyCompactions}
              colors={colors()}
              maxWidth={32}
            />
            <box flexDirection="column" width="100%" paddingTop={1}>
              <Show when={dashboard().compactions.count > 0}>
                <text fg={colors().faint} height={1} wrapMode="none" truncate>
                  {dashboard().compactions.count} COMPACTIONS
                  {dashboard().compactions.lastAt
                    ? ` · ${formatAgo(
                        state.now() - dashboard().compactions.lastAt!,
                      )}`
                    : ""}
                </text>
              </Show>
              <Show when={options().tui?.compact !== false}>
                <box
                  height={1}
                  flexShrink={0}
                  onMouseDown={(event) => {
                    if (event.button !== 0) return
                    compact()
                  }}
                >
                  <text selectable={false} fg={colors().accent} height={1} wrapMode="none">
                    {compacting() ? "COMPACTING…" : "COMPACT"}
                  </text>
                </box>
              </Show>
            </box>

            <SectionBreak colors={colors()} />
            <box
              flexDirection="row"
              justifyContent="space-between"
              width="100%"
              height={1}
            >
              <text fg={colors().muted}>02 TOKENS</text>
            </box>
            <box flexDirection="column" width="100%">
              <MiniStat
                label="INPUT"
                value={formatTokens(dashboard().usage.input)}
                colors={colors()}
              />
              <MiniStat
                label="OUTPUT"
                value={formatTokens(dashboard().usage.output)}
                colors={colors()}
              />
              <MiniStat
                label="CACHE READ"
                value={formatTokens(dashboard().usage.cacheRead)}
                colors={colors()}
              />
              <MiniStat
                label="CACHE WRITE"
                value={formatTokens(dashboard().usage.cacheWrite)}
                colors={colors()}
              />
              <MiniStat
                label="CACHE HIT"
                value={formatPercent(dashboard().usage.cacheHitRate, 0)}
                colors={colors()}
                accent
              />
              <MiniStat
                label="COST"
                value={formatCost(dashboard().totalCost)}
                colors={colors()}
              />
              <MiniStat
                label="GENERATION"
                value={`${formatSpeed(dashboard().speed.generation)} t/s`}
                colors={colors()}
              />
              <MiniStat
                label="PREFILL"
                value={`${formatSpeed(dashboard().speed.prefill)} t/s`}
                colors={colors()}
              />
            </box>

            <SectionBreak colors={colors()} />
            <box
              flexDirection="row"
              justifyContent="space-between"
              width="100%"
              height={1}
            >
              <text fg={colors().muted}>03 AGENTS</text>
              <text fg={colors().faint}>
                {agents().total > 0
                  ? `${agents().running} RUN · ${agents().total}`
                  : "NONE"}
              </text>
            </box>
            <Show
              when={agents().visible.length > 0}
              fallback={<text fg={colors().faint}>NO SUBAGENTS</text>}
            >
              <For each={agents().visible}>
                {(node) => (
                  <SidebarAgent
                    node={node}
                    colors={colors()}
                    now={state.now()}
                    onOpen={() => openSession(context, node.session.id)}
                  />
                )}
              </For>
              <Show when={agents().hidden > 0}>
                <text fg={colors().faint} height={1}>
                  +{agents().hidden} MORE · {agents().total} TOTAL
                </text>
              </Show>
            </Show>

            <text fg={colors().faint} height={1} marginTop={1} wrapMode="none" truncate>
              UPDATED {formatAgo(state.now() - state.lastUpdate())}
            </text>
          </box>
        )}
      </Show>
    </box>
  )
}

function SectionBreak(props: { colors: Colors }) {
  return (
    <box width="100%" paddingTop={1}>
      <Rule colors={props.colors} />
    </box>
  )
}

function MiniStat(props: {
  label: string
  value: string
  colors: Colors
  accent?: boolean
}) {
  return (
    <box flexDirection="row" justifyContent="space-between" width="100%" height={1}>
      <text fg={props.colors.faint}>{props.label}</text>
      <text fg={props.accent ? props.colors.accent : props.colors.text}>
        {props.value}
      </text>
    </box>
  )
}

function SidebarAgent(props: {
  node: SubagentNode
  colors: Colors
  now: number
  onOpen: () => void
}) {
  const running = () => props.node.status === "running"
  const elapsed = () =>
    running() && props.node.workingSince
      ? formatDuration(props.now - props.node.workingSince)
      : formatAgo(props.now - props.node.session.time.updated)
  const prefix = () => (props.node.depth > 1 ? "└ ".repeat(props.node.depth - 1) : "")

  return (
    <box
      flexDirection="row"
      width="100%"
      height={1}
      onMouseUp={(event) => {
        if (event.button !== 0) return
        props.onOpen()
      }}
    >
      <text selectable={false} fg={running() ? props.colors.accent : props.colors.faint}>
        {running() ? "●" : "○"}
      </text>
      <box flexGrow={1} height={1} minWidth={0}>
        <text
          selectable={false}
          fg={running() ? props.colors.text : props.colors.muted}
          height={1}
          wrapMode="none"
          truncate
        >
          {" "}
          {prefix()}
          {props.node.session.title ?? props.node.session.agent ?? "subagent"}
        </text>
      </box>
      <text selectable={false} fg={props.colors.faint}>{elapsed()}</text>
      <Show when={props.node.context?.percent !== undefined}>
        <text
          selectable={false}
          fg={toneColor(props.colors, toneFor(props.node.context?.percent))}
        >
          {" "}
          {formatPercent(props.node.context?.percent, 0)}
        </text>
      </Show>
    </box>
  )
}
