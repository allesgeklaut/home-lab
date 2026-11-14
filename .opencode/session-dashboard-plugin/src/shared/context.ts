import type {
  ContextSummary,
  DashboardOptions,
  ModelInfo,
  ModelLimit,
  SessionMessage,
} from "./types.js"
import { promptTokens } from "./usage.js"

const RESERVE_MIN = 16_000
const RESERVE_SHARE = 0.15

export function inputWindow(limit: ModelLimit | undefined): number {
  if (!limit) return 0
  const context = positive(limit.context)
  const input = positive(limit.input)
  if (input && context) return Math.min(input, context)
  return input || context
}

export function bufferTokens(window: number, buffer?: number | string): number {
  const fallback = Math.max(
    Math.floor(window * RESERVE_SHARE),
    window >= 2 * RESERVE_MIN ? RESERVE_MIN : 0,
  )
  if (window <= 0) return 0
  if (buffer === undefined) return fallback

  const tokens =
    typeof buffer === "string"
      ? Math.round((window * Number.parseFloat(buffer)) / 100)
      : buffer

  if (Number.isFinite(tokens) && tokens >= 0 && tokens < window) return tokens
  return fallback
}

export function compactionCeiling(limit: ModelLimit | undefined, buffer?: number | string): number {
  const window = inputWindow(limit)
  if (window <= 0) return Number.POSITIVE_INFINITY
  return window - bufferTokens(window, buffer)
}

/**
 * The assistant step whose tokens fill the context window now: the latest
 * assistant message after the last completed compaction and before the revert
 * boundary. Mirrors `lastAssistantWithUsage` from `@opencode/client/solid`.
 */
export function lastAssistantWithUsage(
  messages: ReadonlyArray<SessionMessage>,
  boundary?: string,
): SessionMessage | undefined {
  const end = boundaryEnd(messages, boundary)
  if (end === undefined) return undefined

  const compactionIndex = messages.findLastIndex(
    (message, index) =>
      message.type === "compaction" && message.status === "completed" && index < end,
  )

  return messages.findLast(
    (message, index) =>
      message.type === "assistant" &&
      message.tokens !== undefined &&
      index > compactionIndex &&
      index < end,
  )
}

export type ContextTimeline = {
  values: number[]
  /** Indices in `values` of the reset point at a completed compaction. */
  compactions: number[]
}

/**
 * The context-window fill over the session's assistant steps. Each segment
 * starts at zero, and a completed compaction resets the line to zero before
 * the next step, so the graph shows the fill before and after each compaction
 * instead of one continuous line.
 */
export function contextTimeline(
  messages: ReadonlyArray<SessionMessage>,
  boundary?: string,
  limit = 0,
): ContextTimeline {
  const end = boundaryEnd(messages, boundary)
  if (end === undefined) return { values: [], compactions: [] }

  const segments: Array<{ values: number[]; compaction: boolean }> = []
  let current: { values: number[]; compaction: boolean } = {
    values: [],
    compaction: false,
  }

  for (let index = 0; index < end; index += 1) {
    const message = messages[index]
    if (message.type === "compaction" && message.status === "completed") {
      if (current.values.length > 0) {
        segments.push(current)
        current = { values: [], compaction: true }
      } else {
        current.compaction = true
      }
      continue
    }
    if (message.type === "assistant" && message.tokens !== undefined) {
      current.values.push(promptTokens(message.tokens))
    }
  }
  if (current.values.length > 0) segments.push(current)
  if (segments.length === 0) return { values: [], compactions: [] }

  const points: Array<{ value: number; compaction: boolean }> = []
  for (const segment of segments) {
    points.push({ value: 0, compaction: segment.compaction })
    for (const value of segment.values) {
      points.push({ value, compaction: false })
    }
  }

  const window = limit > 0 ? points.slice(-limit) : points
  return {
    values: window.map((point) => point.value),
    compactions: window.flatMap((point, index) =>
      point.compaction ? [index] : [],
    ),
  }
}

export type CompactionHistory = {
  count: number
  lastAt?: number
}

/** Completed compactions before the revert boundary, oldest first. */
export function compactionHistory(
  messages: ReadonlyArray<SessionMessage>,
  boundary?: string,
): CompactionHistory {
  const end = boundaryEnd(messages, boundary)
  if (end === undefined) return { count: 0 }

  const compactions = messages
    .slice(0, end)
    .filter(
      (message) => message.type === "compaction" && message.status === "completed",
    )

  return {
    count: compactions.length,
    lastAt: compactions[compactions.length - 1]?.time.created,
  }
}

export function boundaryEnd(
  messages: ReadonlyArray<SessionMessage>,
  boundary?: string,
): number | undefined {
  if (!boundary) return messages.length
  const index = messages.findIndex((message) => message.id === boundary)
  return index === -1 ? undefined : index
}

export function contextSummary(input: {
  limit: ModelLimit | undefined
  messages: ReadonlyArray<SessionMessage>
  boundary?: string
  options?: DashboardOptions
  model?: ModelInfo
}): ContextSummary {
  const { limit, messages, boundary, options, model } = input
  const rawContextLimit = positive(limit?.context)
  const inputLimit = positive(limit?.input)
  const mode = options?.contextLimit ?? "input-window"

  let window = inputWindow(limit)
  let source: ContextSummary["source"] =
    inputLimit && rawContextLimit ? "min" : inputLimit ? "input" : rawContextLimit ? "context" : "unknown"

  if (typeof mode === "number" && mode > 0) {
    window = mode
    source = "override"
  } else if (mode === "context" && rawContextLimit > 0) {
    window = rawContextLimit
    source = "context"
  }

  const step = lastAssistantWithUsage(messages, boundary)
  const used = step?.tokens ? promptTokens(step.tokens) : 0
  const ceiling = compactionCeiling(limit, options?.compactionBuffer)
  const compaction = model?.settings?.compaction?.type ?? "summary"

  return {
    used,
    limit: window,
    rawContextLimit,
    inputLimit,
    percent: window > 0 ? used / window : undefined,
    ceiling,
    ceilingPercent: window > 0 && Number.isFinite(ceiling) ? ceiling / window : undefined,
    remaining: Number.isFinite(ceiling) ? Math.max(0, ceiling - used) : undefined,
    compaction,
    source,
  }
}

function positive(value: number | undefined): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : 0
}
