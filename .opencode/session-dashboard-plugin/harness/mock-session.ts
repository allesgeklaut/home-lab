const MINUTE = 60_000
const now = Date.now()

type MockSession = {
  id: string
  parentID?: string
  title: string
  agent: string
  model: { providerID: string; id: string; variant?: string }
  cost: number
  tokens: {
    input: number
    output: number
    reasoning: number
    cache: { read: number; write: number }
  }
  time: { created: number; updated: number; busy?: number; idle?: number }
  outcome?: "succeeded" | "failed" | "interrupted"
}

const models = [
  {
    id: "zai-glm-5-3",
    modelID: "zai-glm-5-3",
    providerID: "mistral",
    name: "GLM-5.3",
    limit: { context: 400_000, input: 307_200, output: 128_000 },
    variants: [{ id: "high" }, { id: "max" }],
    settings: { compaction: { type: "summary" } },
  },
  {
    id: "deepseek-v4-1-flash-1017",
    modelID: "deepseek-v4-1-flash-1017",
    providerID: "mistral",
    name: "DeepSeek V4.1 Flash",
    limit: { context: 200_000, input: 160_000, output: 64_000 },
    variants: [{ id: "high" }, { id: "max" }],
    settings: { compaction: { type: "summary" } },
  },
  {
    id: "mistral-large-4",
    modelID: "mistral-large-4",
    providerID: "mistral",
    name: "Mistral Large 4",
    limit: { context: 256_000, input: 192_000, output: 64_000 },
    variants: [{ id: "medium" }, { id: "high" }],
    settings: { compaction: { type: "summary" } },
  },
]

const sessions: Record<string, MockSession> = {
  ses_parent: {
    id: "ses_parent",
    title: "Engram functionality",
    agent: "build",
    model: { providerID: "mistral", id: "zai-glm-5-3", variant: "max" },
    cost: 0.04251096,
    tokens: {
      input: 9_522,
      output: 2_956,
      reasoning: 0,
      cache: { read: 116_928, write: 0 },
    },
    time: { created: now - 30 * MINUTE, updated: now - 2 * MINUTE },
    outcome: "succeeded",
  },
  ses_child_running: {
    id: "ses_child_running",
    parentID: "ses_parent",
    title: "Audit memory retrieval paths",
    agent: "explore",
    model: { providerID: "mistral", id: "deepseek-v4-1-flash-1017", variant: "max" },
    cost: 0.0084,
    tokens: {
      input: 3_200,
      output: 900,
      reasoning: 0,
      cache: { read: 12_000, write: 0 },
    },
    time: { created: now - 8 * MINUTE, updated: now - 20_000, busy: now - 42_000 },
  },
  ses_child_idle: {
    id: "ses_child_idle",
    parentID: "ses_parent",
    title: "Review compaction boundary math",
    agent: "general",
    model: { providerID: "mistral", id: "mistral-large-4", variant: "high" },
    cost: 0.0021,
    tokens: {
      input: 1_200,
      output: 400,
      reasoning: 0,
      cache: { read: 4_000, write: 0 },
    },
    time: { created: now - 18 * MINUTE, updated: now - 6 * MINUTE },
    outcome: "succeeded",
  },
  ses_grandchild: {
    id: "ses_grandchild",
    parentID: "ses_child_running",
    title: "Trace retrieval cache keys",
    agent: "explore",
    model: { providerID: "mistral", id: "deepseek-v4-1-flash-1017", variant: "high" },
    cost: 0.0007,
    tokens: {
      input: 900,
      output: 220,
      reasoning: 0,
      cache: { read: 2_400, write: 0 },
    },
    time: { created: now - 4 * MINUTE, updated: now - 30_000 },
    outcome: "succeeded",
  },
}

function assistant(id: string, prompt: number, created: number) {
  return {
    id,
    type: "assistant",
    time: {
      created,
      firstToken: created + 1_500,
      streamed: created + 4_500,
      completed: created + 4_600,
    },
    model: { providerID: "mistral", id: "zai-glm-5-3", variant: "max" },
    tokens: {
      input: 2_000,
      output: 300,
      reasoning: 0,
      cache: { read: prompt - 2_000, write: 0 },
    },
    cost: 0.002,
    status: "completed",
  }
}

function compaction(id: string, created: number) {
  return {
    id,
    type: "compaction" as const,
    status: "completed" as const,
    time: { created },
  }
}

// A sawtooth that fills, compacts, and fills again, so the sparkline shows a
// real trend and the compaction markers have something to mark.
const parentHistory = Array.from(
  { length: 21 },
  (_, index) => 40_000 + (index % 7) * 30_000 + index * 500,
)
const parentCompactions = new Set([6, 13])

const parentMessages = parentHistory.flatMap((prompt, index) => {
  const created = now - (parentHistory.length - index) * MINUTE
  const message = assistant(`msg_parent_${index}`, prompt, created)
  return parentCompactions.has(index)
    ? [message, compaction(`msg_parent_compact_${index}`, created + 30_000)]
    : [message]
})

const messages: Record<
  string,
  Array<ReturnType<typeof assistant> | ReturnType<typeof compaction>>
> = {
  ses_parent: parentMessages,
  ses_child_running: [
    assistant("msg_child_running_0", 18_000, now - 6 * MINUTE),
    assistant("msg_child_running_1", 28_000, now - 4 * MINUTE),
    assistant("msg_child_running_2", 40_000, now - 20_000),
  ],
  ses_child_idle: [
    assistant("msg_child_idle_0", 8_000, now - 12 * MINUTE),
    assistant("msg_child_idle_1", 12_000, now - 6 * MINUTE),
  ],
  ses_grandchild: [
    assistant("msg_grandchild_0", 3_300, now - 3 * MINUTE),
    assistant("msg_grandchild_1", 4_100, now - 30_000),
  ],
}

for (let index = 0; index < 8; index += 1) {
  const id = `ses_extra_${index}`
  sessions[id] = {
    id,
    parentID: "ses_parent",
    title: `Background review ${index + 1}`,
    agent: index % 2 === 0 ? "general" : "explore",
    model: {
      providerID: "mistral",
      id: index % 2 === 0 ? "mistral-large-4" : "deepseek-v4-1-flash-1017",
      variant: "high",
    },
    cost: 0.001 * (index + 1),
    tokens: {
      input: 500 + index * 100,
      output: 100,
      reasoning: 0,
      cache: { read: 1_000 + index * 200, write: 0 },
    },
    time: { created: now - (20 - index) * MINUTE, updated: now - (index + 1) * MINUTE },
    outcome: "succeeded",
  }
  messages[id] = [
    assistant(`msg_extra_${index}`, 4_000 + index * 1_000, now - (index + 1) * MINUTE),
  ]
}

const data = {
  session: {
    get: (id: string) => sessions[id],
    root: () => "ses_parent",
    family: () => Object.keys(sessions),
    status: (id: string) => (id === "ses_child_running" ? "running" : "idle"),
    missing: () => false,
    sync: async () => undefined,
    message: {
      list: (id: string) => messages[id] ?? [],
      sync: async () => undefined,
    },
  },
  location: {
    model: {
      list: () => models,
      sync: async () => undefined,
    },
  },
}

export const mockSession = {
  id: "ses_parent",
  key: "sidecar\nses_parent",
  server: { id: "sidecar", name: "sidecar", data },
}
