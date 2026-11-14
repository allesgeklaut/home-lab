import type {
  DashboardModel,
  DashboardOptions,
  ModelInfo,
  ModelRef,
  SessionInfo,
  SessionMessage,
  SessionStatus,
  SubagentNode,
} from "./types.js"
import { compactionHistory, contextSummary, contextTimeline } from "./context.js"
import { speedSummary } from "./speed.js"
import { summarizeUsage } from "./usage.js"

export type DashboardSource = {
  session: SessionInfo
  status: SessionStatus
  sessions: ReadonlyArray<SessionInfo>
  model: (ref: ModelRef | undefined) => ModelInfo | undefined
  statusOf: (sessionID: string) => SessionStatus
  messages: (sessionID: string) => ReadonlyArray<SessionMessage>
  options?: DashboardOptions
}

export function workingSince(session: SessionInfo): number | undefined {
  const { busy, idle } = session.time
  if (busy === undefined || (idle !== undefined && idle > busy)) return undefined
  return busy
}

export function childrenOf(
  sessions: ReadonlyArray<SessionInfo>,
  parentID: string,
): SessionInfo[] {
  return sessions.filter((session) => session.parentID === parentID)
}

export function buildDashboardModel(source: DashboardSource): DashboardModel {
  const { session, options } = source
  const model = source.model(session.model)
  const subagents = buildSubagentTree(source, session.id)
  const subagentCost = sumCost(subagents)
  const messages = source.messages(session.id)
  const timeline = contextTimeline(messages, session.revert?.messageID)

  return {
    session,
    status: source.status,
    model,
    workingSince: source.status === "running" ? workingSince(session) : undefined,
    usage: summarizeUsage(session.tokens, session.cost),
    subagentCost,
    totalCost: session.cost + subagentCost,
    context: contextSummary({
      limit: model?.limit,
      messages,
      boundary: session.revert?.messageID,
      options,
      model,
    }),
    speed: speedSummary(messages, session.revert?.messageID),
    compactions: compactionHistory(messages, session.revert?.messageID),
    history: timeline.values,
    historyCompactions: timeline.compactions,
    subagents,
    runningSubagents: countRunning(subagents),
    updatedAt: Date.now(),
  }
}

export function buildSubagentTree(
  source: DashboardSource,
  parentID: string,
  depth = 0,
): SubagentNode[] {
  const maxDepth = source.options?.maxSubagentDepth ?? 2
  const children = childrenOf(source.sessions, parentID).sort((left, right) => {
    const leftRunning = source.statusOf(left.id) === "running" ? 0 : 1
    const rightRunning = source.statusOf(right.id) === "running" ? 0 : 1
    if (leftRunning !== rightRunning) return leftRunning - rightRunning

    const leftSince = workingSince(left) ?? Number.POSITIVE_INFINITY
    const rightSince = workingSince(right) ?? Number.POSITIVE_INFINITY
    if (leftSince !== rightSince) return leftSince - rightSince

    return right.time.updated - left.time.updated
  })

  return children.map((session) => {
    const status = source.statusOf(session.id)
    const model = source.model(session.model)
    const context =
      source.options?.showSubagentContext === false
        ? undefined
        : contextSummary({
            limit: model?.limit,
            messages: source.messages(session.id),
            boundary: session.revert?.messageID,
            options: source.options,
            model,
          })

    return {
      session,
      status,
      model,
      workingSince: status === "running" ? workingSince(session) : undefined,
      usage: summarizeUsage(session.tokens, session.cost),
      context,
      depth: depth + 1,
      children: depth < maxDepth ? buildSubagentTree(source, session.id, depth + 1) : [],
    }
  })
}

export function flattenSubagents(nodes: ReadonlyArray<SubagentNode>): SubagentNode[] {
  return nodes.flatMap((node) => [node, ...flattenSubagents(node.children)])
}

export type VisibleSubagents = {
  visible: SubagentNode[]
  hidden: number
  total: number
  running: number
}

/**
 * The subagents worth showing in a long session: every running agent first,
 * then the most recently active idle agents, capped at `max`. A `max` of 0
 * shows everything.
 */
export function visibleSubagents(
  nodes: ReadonlyArray<SubagentNode>,
  max = 8,
): VisibleSubagents {
  const flat = flattenSubagents(nodes)
  const running = flat
    .filter((node) => node.status === "running")
    .sort(
      (left, right) =>
        (left.workingSince ?? Number.POSITIVE_INFINITY) -
        (right.workingSince ?? Number.POSITIVE_INFINITY),
    )
  const idle = flat
    .filter((node) => node.status !== "running")
    .sort((left, right) => right.session.time.updated - left.session.time.updated)
  const ordered = [...running, ...idle]
  const limit = Number.isFinite(max) ? Math.max(0, Math.floor(max)) : 0
  const visible = limit > 0 ? ordered.slice(0, limit) : ordered

  return {
    visible,
    hidden: ordered.length - visible.length,
    total: ordered.length,
    running: running.length,
  }
}

export function sumCost(nodes: ReadonlyArray<SubagentNode>): number {
  return nodes.reduce((total, node) => total + node.session.cost + sumCost(node.children), 0)
}

export function countRunning(nodes: ReadonlyArray<SubagentNode>): number {
  return nodes.reduce(
    (total, node) =>
      total + (node.status === "running" ? 1 : 0) + countRunning(node.children),
    0,
  )
}
