import { For, Show } from "solid-js"
import type { ColorInput } from "@opentui/core"
import { barSegments, toneFor } from "../shared/design.js"
import {
  formatAgo,
  formatCost,
  formatDuration,
  formatModelShort,
  formatPercent,
  formatTokens,
} from "../shared/format.js"
import type { SubagentNode } from "../shared/types.js"
import type { Colors } from "./theme.js"
import { toneColor } from "./theme.js"

export function Rule(props: { colors: Colors }) {
  return <box height={1} width="100%" border={["bottom"]} borderColor={props.colors.line} />
}

export function SectionTitle(props: {
  index: string
  title: string
  right?: string
  colors: Colors
}) {
  return (
    <box flexDirection="row" justifyContent="space-between" width="100%" height={1}>
      <text fg={props.colors.muted}>
        {props.index} {props.title.toUpperCase()}
      </text>
      <Show when={props.right}>
        <text fg={props.colors.faint}>{props.right}</text>
      </Show>
    </box>
  )
}

export function Stat(props: {
  label: string
  value: string
  colors: Colors
  accent?: boolean
}) {
  return (
    <box flexDirection="row" justifyContent="space-between" width="100%" height={1}>
      <text fg={props.colors.faint}>{props.label.toUpperCase()}</text>
      <text fg={props.accent ? props.colors.accent : props.colors.text}>{props.value}</text>
    </box>
  )
}

export function Bar(props: {
  ratio: number | undefined
  colors: Colors
  cells?: number
}) {
  const segments = () => barSegments(props.ratio, props.cells ?? 24)
  const color = () => toneColor(props.colors, toneFor(props.ratio))

  return (
    <box flexDirection="row" height={1}>
      <text fg={color()}>{"█".repeat(segments().filled)}</text>
      <text fg={props.colors.line}>{"░".repeat(segments().empty)}</text>
    </box>
  )
}

export function Sparkline(props: {
  values: ReadonlyArray<number>
  compactions?: ReadonlyArray<number>
  colors: Colors
  maxWidth?: number
}) {
  const blocks = "▁▂▃▄▅▆▇█"
  const window = () => {
    const limit =
      props.maxWidth !== undefined && props.maxWidth > 0
        ? props.maxWidth
        : props.values.length
    const start = Math.max(0, props.values.length - limit)
    return {
      values: props.values.slice(start),
      compactions: (props.compactions ?? [])
        .filter((index) => index >= start)
        .map((index) => index - start),
    }
  }
  const text = () => {
    const { values, compactions } = window()
    if (values.length === 0) return ""
    const max = Math.max(...values, 1)
    const marks = new Set(compactions)
    return values
      .map((value, index) => {
        const ratio = Math.max(0, Math.min(1, value / max))
        const block =
          blocks[Math.min(blocks.length - 1, Math.floor(ratio * blocks.length))]
        return marks.has(index) ? `╎${block}` : block
      })
      .join("")
  }

  return (
    <Show when={text()}>
      <text fg={props.colors.muted} height={1} wrapMode="none">
        {text()}
      </text>
    </Show>
  )
}

export function AgentRow(props: {
  node: SubagentNode
  selected: boolean
  colors: Colors
  now: number
  onOpen?: () => void
}) {
  const running = () => props.node.status === "running"
  const elapsed = () =>
    running() && props.node.workingSince
      ? formatDuration(props.now - props.node.workingSince)
      : formatAgo(props.now - props.node.session.time.updated)
  const context = () => props.node.context

  return (
    <box
      flexDirection="column"
      width="100%"
      border={["left"]}
      borderColor={props.selected ? props.colors.accent : props.colors.line}
      paddingLeft={1}
      gap={0}
      onMouseDown={() => props.onOpen?.()}
    >
      <box flexDirection="row" justifyContent="space-between" width="100%" height={1}>
        <box flexDirection="row" gap={0}>
          <Show when={props.node.depth > 0}>
            <text fg={props.colors.faint}>
              {"│ ".repeat(props.node.depth - 1)}└{" "}
            </text>
          </Show>
          <text fg={props.selected ? props.colors.accent : props.colors.muted}>
            {props.selected ? "› " : "  "}
          </text>
          <text fg={running() ? props.colors.accent : props.colors.faint}>
            {running() ? "●" : "○"}
          </text>
          <text fg={props.colors.text}> {props.node.session.agent ?? "subagent"}</text>
          <Show when={props.node.session.title}>
            <text fg={props.colors.muted}> {props.node.session.title}</text>
          </Show>
        </box>
        <text fg={props.colors.muted}>{elapsed()}</text>
      </box>
      <box flexDirection="row" justifyContent="space-between" width="100%" height={1}>
        <text fg={props.colors.faint}>
          {formatModelShort(props.node.session.model, props.node.model)}
        </text>
        <text fg={props.colors.muted}>
          {formatTokens(props.node.usage.total)} · {formatCost(props.node.session.cost)}
        </text>
      </box>
      <Show when={context()}>
        <box flexDirection="row" justifyContent="space-between" width="100%" height={1}>
          <Bar ratio={context()?.percent} cells={8} colors={props.colors} />
          <text fg={toneColor(props.colors, toneFor(context()?.percent))}>
            {formatPercent(context()?.percent, 0)}
          </text>
        </box>
      </Show>
    </box>
  )
}

export function Dot(props: { color: ColorInput; children?: string }) {
  return <text fg={props.color}>{props.children ?? "●"}</text>
}
