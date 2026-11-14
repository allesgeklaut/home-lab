import assert from "node:assert/strict"
import test from "node:test"
import {
  bufferTokens,
  compactionCeiling,
  compactionHistory,
  contextSummary,
  contextTimeline,
  inputWindow,
  lastAssistantWithUsage,
} from "../shared/context.js"
import type { SessionMessage, TokenUsage } from "../shared/types.js"

const tokens = (input: number, read = 0, write = 0): TokenUsage => ({
  input,
  output: 0,
  reasoning: 0,
  cache: { read, write },
})

const assistant = (id: string, usage?: TokenUsage): SessionMessage => ({
  id,
  type: "assistant",
  time: { created: 1 },
  tokens: usage,
})

const compaction = (id: string): SessionMessage => ({
  id,
  type: "compaction",
  status: "completed",
  time: { created: 1 },
})

test("inputWindow prefers the smaller of input and context", () => {
  assert.equal(inputWindow({ context: 200, input: 100, output: 10 }), 100)
  assert.equal(inputWindow({ context: 200, output: 10 }), 200)
  assert.equal(inputWindow({ context: 0, input: 100, output: 10 }), 100)
  assert.equal(inputWindow(undefined), 0)
})

test("bufferTokens uses 15% with a 16k floor for large windows", () => {
  assert.equal(bufferTokens(1_000_000), 150_000)
  assert.equal(bufferTokens(100_000), 16_000)
  assert.equal(bufferTokens(20_000), 3_000)
  assert.equal(bufferTokens(100_000, "20%"), 20_000)
  assert.equal(bufferTokens(100_000, 50_000), 50_000)
  assert.equal(bufferTokens(100_000, 200_000), 16_000)
})

test("compactionCeiling subtracts the buffer", () => {
  assert.equal(compactionCeiling({ context: 100_000, output: 10_000 }), 84_000)
  assert.equal(
    compactionCeiling({ context: 100_000, input: 80_000, output: 10_000 }),
    64_000,
  )
})

test("lastAssistantWithUsage ignores steps before the last completed compaction", () => {
  const messages = [
    assistant("old", tokens(10)),
    compaction("compact"),
    assistant("new", tokens(20, 30)),
  ]
  assert.equal(lastAssistantWithUsage(messages)?.id, "new")
})

test("lastAssistantWithUsage stops at the revert boundary", () => {
  const messages = [
    assistant("before", tokens(10)),
    assistant("revert", tokens(20)),
    assistant("after", tokens(30)),
  ]
  assert.equal(lastAssistantWithUsage(messages, "revert")?.id, "before")
})

test("lastAssistantWithUsage returns undefined without a measured step", () => {
  assert.equal(lastAssistantWithUsage([assistant("empty")]), undefined)
})

test("contextTimeline starts at zero and resets at each compaction", () => {
  const messages = [
    assistant("a1", tokens(10)),
    assistant("a2", tokens(20)),
    compaction("c1"),
    assistant("a3", tokens(5)),
    assistant("a4", tokens(15)),
  ]
  const timeline = contextTimeline(messages)
  assert.deepEqual(timeline.values, [0, 10, 20, 0, 5, 15])
  assert.deepEqual(timeline.compactions, [3])
})

test("contextTimeline caps to the most recent points and keeps markers aligned", () => {
  const messages = [
    assistant("a1", tokens(10)),
    compaction("c1"),
    assistant("a2", tokens(20)),
    assistant("a3", tokens(30)),
  ]
  const timeline = contextTimeline(messages, undefined, 3)
  assert.deepEqual(timeline.values, [0, 20, 30])
  assert.deepEqual(timeline.compactions, [0])
})

test("contextTimeline stops at the revert boundary", () => {
  const messages = [
    assistant("a1", tokens(10)),
    assistant("a2", tokens(20)),
    assistant("a3", tokens(30)),
  ]
  const timeline = contextTimeline(messages, "a3")
  assert.deepEqual(timeline.values, [0, 10, 20])
})

test("compactionHistory counts completed compactions and reports the last one", () => {
  const messages = [
    compaction("c1"),
    assistant("a1", tokens(10)),
    { ...compaction("c2"), time: { created: 42 } },
    assistant("a2", tokens(20)),
    { ...compaction("c3"), status: "failed" as const },
  ]
  const history = compactionHistory(messages)
  assert.equal(history.count, 2)
  assert.equal(history.lastAt, 42)
  assert.equal(compactionHistory([]).count, 0)
  assert.equal(compactionHistory([]).lastAt, undefined)
})

test("contextSummary uses the configured input window by default", () => {
  const summary = contextSummary({
    limit: { context: 200, input: 100, output: 10 },
    messages: [assistant("a", tokens(50, 10, 0))],
  })
  assert.equal(summary.used, 60)
  assert.equal(summary.limit, 100)
  assert.equal(summary.percent, 0.6)
  assert.equal(summary.remaining, 25)
  assert.equal(summary.source, "min")
  assert.equal(summary.rawContextLimit, 200)
  assert.equal(summary.inputLimit, 100)
})

test("contextSummary can use the raw context window", () => {
  const summary = contextSummary({
    limit: { context: 200, input: 100, output: 10 },
    messages: [assistant("a", tokens(50))],
    options: { contextLimit: "context" },
  })
  assert.equal(summary.limit, 200)
  assert.equal(summary.source, "context")
})

test("contextSummary supports an explicit override", () => {
  const summary = contextSummary({
    limit: { context: 200, output: 10 },
    messages: [assistant("a", tokens(50))],
    options: { contextLimit: 80 },
  })
  assert.equal(summary.limit, 80)
  assert.equal(summary.source, "override")
})

test("contextSummary reports native compaction from the model settings", () => {
  const summary = contextSummary({
    limit: { context: 100_000, output: 10_000 },
    messages: [],
    model: {
      id: "m",
      modelID: "m",
      providerID: "p",
      name: "m",
      limit: { context: 100_000, output: 10_000 },
      settings: { compaction: { type: "native" } },
    },
  })
  assert.equal(summary.compaction, "native")
})
