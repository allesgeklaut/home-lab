import { For, Show, createEffect, createMemo, createSignal, onCleanup } from "solid-js"
import type { Accessor } from "solid-js"
import { usePlugin } from "@opencode/plugin/gui"
import type { MountedSession } from "@opencode/plugin/gui"
import { barSegments, toneFor } from "../shared/design.js"
import {
  formatAgo,
  formatCost,
  formatDuration,
  formatModel,
  formatModelShort,
  formatPercent,
  formatSpeed,
  formatTokens,
} from "../shared/format.js"
import { visibleSubagents } from "../shared/sessions.js"
import type {
  DashboardModel,
  DashboardOptions,
  ModelInfo,
  ModelRef,
  SubagentNode,
} from "../shared/types.js"
import { buildGuiModel, syncGuiSession } from "./data.js"

export function Dashboard(props: {
  session: MountedSession
  options?: DashboardOptions
}) {
  const ctx = usePlugin()
  const [tick, setTick] = createSignal(0)
  const [lastSync, setLastSync] = createSignal(Date.now())
  const [compacting, setCompacting] = createSignal(false)

  const model = createMemo(() => {
    tick()
    return buildGuiModel(props.session, props.options)
  })
  const agents = createMemo(() =>
    visibleSubagents(model()?.subagents ?? [], props.options?.maxSubagents ?? 8),
  )
  const now = () => {
    tick()
    return Date.now()
  }

  const openSubagent = (node: SubagentNode) => {
    ctx.tabs.open(
      { server: props.session.server.id, id: node.session.id },
      { focus: true },
    )
  }

  const compact = async () => {
    if (compacting()) return
    if (typeof ctx.client?.session?.compact !== "function") {
      ctx.toast?.show?.({
        title: "Session dashboard",
        description: "Compaction API is unavailable in this OpenCode version.",
        variant: "error",
      })
      return
    }
    setCompacting(true)
    try {
      await ctx.client.session.compact({ sessionID: props.session.id })
      ctx.toast?.show?.({
        title: "Session dashboard",
        description: "Compaction requested",
        variant: "success",
      })
    } catch (error) {
      ctx.toast?.show?.({
        title: "Session dashboard",
        description: `Compaction failed: ${
          error instanceof Error ? error.message : String(error)
        }`,
        variant: "error",
      })
    } finally {
      setCompacting(false)
    }
  }

  const refresh = () => {
    void syncGuiSession(props.session)
      .then(() => {
        setLastSync(Date.now())
        setTick((value) => value + 1)
      })
      .catch(() => undefined)
  }

  createEffect(() => {
    const session = props.session
    void syncGuiSession(session)
      .then(() => {
        setLastSync(Date.now())
        setTick((value) => value + 1)
      })
      .catch(() => undefined)
  })

  const timer = setInterval(() => {
    const current = model()
    if (current?.status === "running" || (current?.runningSubagents ?? 0) > 0) {
      setTick((value) => value + 1)
    }
  }, 1000)
  onCleanup(() => clearInterval(timer))

  return (
    <div
      class="sd-root"
      style={{ "font-family": ctx.appearance.font("mono") }}
    >
      <span class="sd-corner tl" />
      <span class="sd-corner tr" />
      <span class="sd-corner bl" />
      <span class="sd-corner br" />

      <div class="sd-scroll">
        <header class="sd-header">
          <div class="sd-brand">
            <div class="sd-mark">//</div>
            <div class="sd-brand-title">
              Session <span>//</span> Telemetry
            </div>
          </div>
          <div class="sd-live">
            <span
              class={`sd-led ${model()?.status === "running" ? "" : "idle"}`}
            />
            {model()?.status === "running" ? "Live" : "Idle"}
          </div>
        </header>

        <Show
          when={model()}
          fallback={<div class="sd-empty">Waiting for session data…</div>}
        >
          {(dashboard: Accessor<DashboardModel>) => (
            <>
              <section class="sd-model">
                <div class="sd-eyebrow">
                  <span>Model</span>
                  <span class="sd-chip accent">Phosphor</span>
                </div>
                <div class="sd-model-name">
                  <ModelName
                    modelRef={dashboard().session.model}
                    model={dashboard().model}
                  />
                </div>
                <div class="sd-meta">
                  <span
                    class={`sd-chip ${
                      dashboard().status === "running" ? "accent" : ""
                    }`}
                  >
                    <span
                      class={`sd-led ${
                        dashboard().status === "running" ? "" : "idle"
                      }`}
                    />
                    {dashboard().status === "running" ? "Running" : "Idle"}
                  </span>
                  <Show when={dashboard().workingSince}>
                    <span>
                      {formatDuration(now() - dashboard().workingSince!)}
                    </span>
                  </Show>
                  <span class="sd-chip">
                    {compactionLabel(dashboard().context.compaction)}
                  </span>
                </div>
              </section>

              <section class="sd-section">
                <div class="sd-eyebrow">
                  <span>
                    <span class="sd-index">01</span> Context
                  </span>
                  <span>Configured budget</span>
                </div>
                <div class="sd-context-head">
                  <div
                    class={`sd-big ${toneClass(dashboard().context.percent)}`}
                  >
                    {percentText(dashboard().context.percent)}
                    <Show when={dashboard().context.percent !== undefined}>
                      <small>%</small>
                    </Show>
                  </div>
                  <div class="sd-context-side">
                    <strong>{formatTokens(dashboard().context.used)}</strong> /{" "}
                    {dashboard().context.limit > 0
                      ? formatTokens(dashboard().context.limit)
                      : "—"}
                    <br />
                    compact at{" "}
                    <strong>
                      {Number.isFinite(dashboard().context.ceiling)
                        ? formatTokens(dashboard().context.ceiling)
                        : "—"}
                    </strong>
                    <br />
                    <strong>
                      {dashboard().context.remaining !== undefined
                        ? formatTokens(dashboard().context.remaining!)
                        : "—"}
                    </strong>{" "}
                    headroom
                    <Show when={props.options?.gui?.compact !== false}>
                      <div class="sd-context-action">
                        <button
                          class="sd-toggle"
                          type="button"
                          title="Request session compaction"
                          disabled={compacting()}
                          onClick={() => void compact()}
                        >
                          <span class="sd-dot" />{" "}
                          {compacting() ? "Compacting…" : "Compact"}
                        </button>
                      </div>
                    </Show>
                  </div>
                </div>
                <Bar
                  ratio={dashboard().context.percent}
                  threshold={dashboard().context.ceilingPercent}
                />
                <div class="sd-legend">
                  <span>0</span>
                  <span>
                    auto-compact{" "}
                    {formatPercent(dashboard().context.ceilingPercent, 0)}
                  </span>
                  <span>100</span>
                </div>
                <Sparkline
                  values={dashboard().history}
                  compactions={dashboard().historyCompactions}
                />
                <Show when={dashboard().compactions.count > 0}>
                  <div class="sd-compactions">
                    {dashboard().compactions.count} compactions
                    {dashboard().compactions.lastAt
                      ? ` · last ${formatAgo(
                          now() - dashboard().compactions.lastAt!,
                        )}`
                      : ""}
                  </div>
                </Show>
              </section>

              <section class="sd-section">
                <div class="sd-eyebrow">
                  <span>
                    <span class="sd-index">02</span> Tokens
                  </span>
                  <span>Session cumulative</span>
                </div>
                <div class="sd-stats">
                  <Stat
                    label="Input"
                    value={formatTokens(dashboard().usage.input)}
                  />
                  <Stat
                    label="Output"
                    value={formatTokens(dashboard().usage.output)}
                  />
                  <Stat
                    label="Cache R"
                    value={formatTokens(dashboard().usage.cacheRead)}
                  />
                  <Stat
                    label="Cache W"
                    value={formatTokens(dashboard().usage.cacheWrite)}
                  />
                  <Stat
                    label="Total"
                    value={formatTokens(dashboard().usage.total)}
                    accent
                  />
                  <Stat
                    label="Hit rate"
                    value={formatPercent(dashboard().usage.cacheHitRate, 0)}
                    accent
                  />
                  <Stat
                    label="Gen t/s"
                    value={formatSpeed(dashboard().speed.generation)}
                  />
                  <Stat
                    label="Prefill t/s"
                    value={formatSpeed(dashboard().speed.prefill)}
                  />
                </div>
                <div class="sd-cost">
                  <span class="sd-label">Cost</span>
                  <span class="sd-cost-value">
                    {formatCost(dashboard().totalCost)}
                  </span>
                  <span class="sd-cost-split">
                    own {formatCost(dashboard().usage.cost)} · subagents{" "}
                    {formatCost(dashboard().subagentCost)}
                  </span>
                </div>
              </section>

              <section class="sd-section">
                <div class="sd-eyebrow">
                  <span>
                    <span class="sd-index">03</span> Subagents
                  </span>
                  <span>
                    {agents().running} running · {agents().total} total
                  </span>
                </div>
                <Show
                  when={agents().visible.length > 0}
                  fallback={<div class="sd-empty">No subagents</div>}
                >
                  <div class="sd-agents">
                    <For each={agents().visible}>
                      {(node) => (
                        <AgentRow
                          node={node}
                          now={now()}
                          onOpen={() => openSubagent(node)}
                        />
                      )}
                    </For>
                    <Show when={agents().hidden > 0}>
                      <div class="sd-empty">
                        +{agents().hidden} more · {agents().total} total
                      </div>
                    </Show>
                  </div>
                </Show>
              </section>

              <footer class="sd-footer">
                <span>Updated {formatAgo(now() - lastSync())}</span>
                <span class="sd-keys">
                  <button
                    class="sd-toggle"
                    type="button"
                    title="Refresh session data"
                    onClick={refresh}
                  >
                    <span class="sd-dot" /> Refresh
                  </button>
                </span>
              </footer>
            </>
          )}
        </Show>
      </div>
    </div>
  )
}

function ModelName(props: { modelRef?: ModelRef; model?: ModelInfo }) {
  const alias = () => {
    const ref = props.modelRef
    if (!ref) return "unknown"
    return `${ref.providerID}/${ref.id}${ref.variant ? `#${ref.variant}` : ""}`
  }
  const target = () => {
    const model = props.model
    if (!model?.modelID || model.modelID === props.modelRef?.id) return undefined
    return model.modelID
  }

  return (
    <>
      <span class="sd-alias">{alias()}</span>
      <Show when={target()}>
        <span class="sd-arrow"> → </span>
        <span>{target()}</span>
      </Show>
    </>
  )
}

function Stat(props: { label: string; value: string; accent?: boolean }) {
  return (
    <div class="sd-stat">
      <span class="sd-label">{props.label}</span>
      <span class={`sd-value ${props.accent ? "accent" : ""}`}>
        {props.value}
      </span>
    </div>
  )
}

function Bar(props: {
  ratio: number | undefined
  threshold?: number
  cells?: number
}) {
  const segments = () => barSegments(props.ratio, props.cells ?? 24)
  const tone = () => toneFor(props.ratio)
  const cellClass = () => {
    if (tone() === "warning") return "sd-cell on warn"
    if (tone() === "danger") return "sd-cell on danger"
    return "sd-cell on"
  }
  const thresholdLeft = () => {
    const value = props.threshold
    if (value === undefined || !Number.isFinite(value)) return undefined
    return `${Math.min(100, Math.max(0, value * 100))}%`
  }

  return (
    <div
      class="sd-bar"
      role="img"
      aria-label={`Context ${formatPercent(props.ratio, 0)}`}
    >
      <For each={Array.from({ length: segments().filled }, (_, i) => i)}>
        {() => <span class={cellClass()} />}
      </For>
      <For each={Array.from({ length: segments().empty }, (_, i) => i)}>
        {() => <span class="sd-cell" />}
      </For>
      <Show when={thresholdLeft()}>
        <span class="sd-threshold" style={{ left: thresholdLeft()! }} />
      </Show>
    </div>
  )
}

function MiniBar(props: { ratio: number | undefined }) {
  const segments = () => barSegments(props.ratio, 12)
  return (
    <div class="sd-mini">
      <For each={Array.from({ length: segments().filled }, (_, i) => i)}>
        {() => <span class="sd-cell on" />}
      </For>
      <For each={Array.from({ length: segments().empty }, (_, i) => i)}>
        {() => <span class="sd-cell" />}
      </For>
    </div>
  )
}

function Sparkline(props: {
  values: ReadonlyArray<number>
  compactions?: ReadonlyArray<number>
}) {
  const geometry = () => {
    const values = props.values
    if (values.length === 0) return undefined
    const max = Math.max(...values, 1)
    const points = values.map((value, index) => {
      const x =
        values.length === 1 ? 100 : (index / (values.length - 1)) * 100
      const y = 30 - Math.max(0, Math.min(1, value / max)) * 28
      return `${x.toFixed(2)},${y.toFixed(2)}`
    })
    const markers = (props.compactions ?? []).map((index) =>
      values.length === 1 ? 100 : (index / (values.length - 1)) * 100,
    )
    const last = points[points.length - 1].split(",")
    return {
      line: points.join(" "),
      area: `0,30 ${points.join(" ")} 100,30`,
      cx: last[0],
      cy: last[1],
      markers,
    }
  }

  return (
    <Show when={geometry()}>
      {(shape: Accessor<{
        line: string
        area: string
        cx: string
        cy: string
        markers: number[]
      }>) => (
        <svg
          class="sd-spark"
          viewBox="0 0 100 30"
          preserveAspectRatio="none"
          aria-hidden="true"
        >
          <polygon class="area" points={shape().area} />
          <For each={shape().markers}>
            {(x) => (
              <line class="marker" x1={x} y1="0" x2={x} y2="30" />
            )}
          </For>
          <polyline class="line" points={shape().line} />
          <circle class="dot" cx={shape().cx} cy={shape().cy} r="1.6" />
        </svg>
      )}
    </Show>
  )
}

function AgentRow(props: {
  node: SubagentNode
  now: number
  onOpen: () => void
}) {
  const running = () => props.node.status === "running"
  const elapsed = () =>
    running() && props.node.workingSince
      ? formatDuration(props.now - props.node.workingSince)
      : formatAgo(props.now - props.node.session.time.updated)

  return (
    <article
      class={`sd-agent ${running() ? "" : "idle"}`}
      role="button"
      tabIndex={0}
      title="Open subagent session"
      style={{ "padding-left": `${8 + props.node.depth * 10}px` }}
      onClick={() => props.onOpen()}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault()
          props.onOpen()
        }
      }}
    >
      <span class="sd-dot" />
      <div>
        <div class="sd-agent-name">
          <Show when={props.node.depth > 0}>
            <span class="sd-agent-tree">
              {"│ ".repeat(props.node.depth - 1)}└
            </span>
          </Show>
          <span>{props.node.session.agent ?? "subagent"}</span>
          <Show when={props.node.session.title}>
            <span class="sd-agent-title">{props.node.session.title}</span>
          </Show>
        </div>
        <div
          class="sd-agent-model"
          title={formatModel(props.node.session.model, props.node.model)}
        >
          {formatModelShort(props.node.session.model, props.node.model)}
        </div>
      </div>
      <div class="sd-agent-right">
        <div class="time">{elapsed()}</div>
        <div>
          {formatTokens(props.node.usage.total)} ·{" "}
          <span class="money">{formatCost(props.node.session.cost)}</span>
        </div>
        <Show when={props.node.context}>
          <MiniBar ratio={props.node.context?.percent} />
        </Show>
      </div>
    </article>
  )
}

function percentText(value: number | undefined): string {
  if (value === undefined || !Number.isFinite(value)) return "—"
  return String(Math.round(value * 100))
}

function toneClass(value: number | undefined): string {
  const tone = toneFor(value)
  if (tone === "warning") return "warn"
  if (tone === "danger") return "danger"
  return ""
}

function compactionLabel(kind: "summary" | "native" | "off"): string {
  if (kind === "native") return "Native compaction"
  if (kind === "off") return "Auto-compact off"
  return "Summary compaction"
}
