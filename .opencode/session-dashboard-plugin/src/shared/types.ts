export type TokenUsage = {
  input: number
  output: number
  reasoning: number
  cache: {
    read: number
    write: number
  }
}

export type ModelRef = {
  providerID: string
  id: string
  variant?: string
}

export type ModelLimit = {
  context: number
  input?: number
  output: number
}

export type ModelInfo = {
  id: string
  modelID: string
  providerID: string
  name: string
  limit: ModelLimit
  variants?: ReadonlyArray<{ id: string }>
  settings?: {
    compaction?: {
      type?: "summary" | "native"
    }
  }
}

export type SessionTime = {
  created: number
  updated: number
  busy?: number
  idle?: number
}

export type SessionInfo = {
  id: string
  parentID?: string
  title?: string
  agent?: string
  model?: ModelRef
  cost: number
  tokens: TokenUsage
  time: SessionTime
  revert?: {
    messageID?: string
  }
  outcome?: "succeeded" | "failed" | "interrupted"
}

export type SessionMessage = {
  id: string
  type: string
  time: {
    created: number
    firstToken?: number
    streamed?: number
    completed?: number
  }
  model?: ModelRef
  tokens?: TokenUsage
  cost?: number
  status?: string
}

export type SessionStatus = "idle" | "running"

export type ContextLimitMode = "input-window" | "context" | number

export type DashboardOptions = {
  refreshMs?: number
  maxSubagentDepth?: number
  maxSubagents?: number
  contextLimit?: ContextLimitMode
  compactionBuffer?: number | string
  showSubagentContext?: boolean
  tui?: {
    panel?: boolean
    footer?: boolean
    compact?: boolean
  }
  gui?: {
    panel?: boolean
    composerPill?: boolean
    compact?: boolean
  }
}

export type UsageSummary = {
  input: number
  output: number
  reasoning: number
  cacheRead: number
  cacheWrite: number
  prompt: number
  total: number
  cacheHitRate?: number
  cost: number
}

export type ContextSummary = {
  used: number
  limit: number
  rawContextLimit: number
  inputLimit?: number
  percent?: number
  ceiling: number
  ceilingPercent?: number
  remaining?: number
  compaction: "summary" | "native" | "off"
  source: "input" | "context" | "min" | "override" | "unknown"
}

export type SpeedSummary = {
  /** Generated output and reasoning tokens per second. */
  generation?: number
  /** Prompt tokens processed per second before the first token, when the host reports it. */
  prefill?: number
}

export type SubagentNode = {
  session: SessionInfo
  status: SessionStatus
  model?: ModelInfo
  workingSince?: number
  usage: UsageSummary
  context?: ContextSummary
  depth: number
  children: SubagentNode[]
}

export type DashboardModel = {
  session: SessionInfo
  status: SessionStatus
  model?: ModelInfo
  workingSince?: number
  usage: UsageSummary
  subagentCost: number
  totalCost: number
  context: ContextSummary
  speed: SpeedSummary
  compactions: {
    count: number
    lastAt?: number
  }
  history: number[]
  historyCompactions: number[]
  subagents: SubagentNode[]
  runningSubagents: number
  updatedAt: number
}
