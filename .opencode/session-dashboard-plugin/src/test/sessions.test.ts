import assert from "node:assert/strict"
import test from "node:test"
import {
  buildDashboardModel,
  buildSubagentTree,
  countRunning,
  sumCost,
  visibleSubagents,
  workingSince,
} from "../shared/sessions.js"
import type {
  ModelInfo,
  SessionInfo,
  SessionMessage,
  TokenUsage,
} from "../shared/types.js"

const tokens = (input: number): TokenUsage => ({
  input,
  output: 0,
  reasoning: 0,
  cache: { read: 0, write: 0 },
})

const session = (
  id: string,
  parentID: string | undefined,
  cost: number,
  input: number,
  time: Partial<SessionInfo["time"]> = {},
): SessionInfo => ({
  id,
  parentID,
  cost,
  tokens: tokens(input),
  time: { created: 1, updated: 10, ...time },
})

const model: ModelInfo = {
  id: "m",
  modelID: "m",
  providerID: "p",
  name: "m",
  limit: { context: 1_000, input: 500, output: 100 },
}

const sessions = [
  session("root", undefined, 0.1, 200),
  session("a", "root", 0.2, 50, { busy: 10 }),
  session("b", "root", 0.3, 70, { busy: 5, idle: 10 }),
  session("c", "a", 0.4, 30, { busy: 20 }),
]

const messages: Record<string, SessionMessage[]> = {
  root: [
    { id: "c1", type: "compaction", status: "completed", time: { created: 1 } },
    { id: "m1", type: "assistant", time: { created: 2 }, tokens: tokens(200) },
  ],
  a: [{ id: "m2", type: "assistant", time: { created: 1 }, tokens: tokens(100) }],
  b: [],
  c: [],
}

const source = {
  session: sessions[0],
  status: "idle" as const,
  sessions,
  model: () => model,
  statusOf: (id: string) => (id === "a" || id === "c" ? "running" : "idle") as const,
  messages: (id: string) => messages[id] ?? [],
  options: { maxSubagentDepth: 2, showSubagentContext: true },
}

test("workingSince returns the current run start", () => {
  assert.equal(workingSince(session("x", undefined, 0, 0, { busy: 10 })), 10)
  assert.equal(workingSince(session("x", undefined, 0, 0, { busy: 10, idle: 20 })), undefined)
  assert.equal(workingSince(session("x", undefined, 0, 0)), undefined)
})

test("buildSubagentTree sorts running first and nests descendants", () => {
  const tree = buildSubagentTree(source, "root")
  assert.equal(tree.length, 2)
  assert.equal(tree[0].session.id, "a")
  assert.equal(tree[1].session.id, "b")
  assert.equal(tree[0].children[0].session.id, "c")
  assert.equal(tree[0].depth, 1)
  assert.equal(tree[0].children[0].depth, 2)
  assert.equal(tree[0].status, "running")
  assert.equal(tree[0].context?.used, 100)
  assert.equal(tree[0].context?.limit, 500)
  assert.equal(tree[0].context?.percent, 0.2)
  assert.equal(tree[1].context?.used, 0)
})

test("countRunning and sumCost include descendants", () => {
  const tree = buildSubagentTree(source, "root")
  assert.equal(countRunning(tree), 2)
  assert.ok(Math.abs(sumCost(tree) - 0.9) < 1e-9)
})

test("visibleSubagents keeps running agents first and caps the list", () => {
  const tree = buildSubagentTree(source, "root")
  const capped = visibleSubagents(tree, 2)
  assert.deepEqual(
    capped.visible.map((node) => node.session.id),
    ["a", "c"],
  )
  assert.equal(capped.hidden, 1)
  assert.equal(capped.total, 3)
  assert.equal(capped.running, 2)

  const all = visibleSubagents(tree, 0)
  assert.equal(all.visible.length, 3)
  assert.equal(all.hidden, 0)
})

test("buildDashboardModel combines session, subagents and context", () => {
  const dashboard = buildDashboardModel(source)
  assert.equal(dashboard.session.id, "root")
  assert.equal(dashboard.usage.prompt, 200)
  assert.equal(dashboard.usage.cost, 0.1)
  assert.ok(Math.abs(dashboard.subagentCost - 0.9) < 1e-9)
  assert.ok(Math.abs(dashboard.totalCost - 1) < 1e-9)
  assert.equal(dashboard.runningSubagents, 2)
  assert.equal(dashboard.context.used, 200)
  assert.equal(dashboard.context.limit, 500)
  assert.equal(dashboard.context.percent, 0.4)
  assert.equal(dashboard.compactions.count, 1)
  assert.deepEqual(dashboard.history, [0, 200])
  assert.deepEqual(dashboard.historyCompactions, [0])
})
