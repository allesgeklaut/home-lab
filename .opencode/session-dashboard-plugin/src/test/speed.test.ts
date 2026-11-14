import assert from "node:assert/strict"
import test from "node:test"
import { speedSummary } from "../shared/speed.js"
import type { SessionMessage, TokenUsage } from "../shared/types.js"

const tokens = (
  input: number,
  output: number,
  reasoning = 0,
  read = 0,
  write = 0,
): TokenUsage => ({
  input,
  output,
  reasoning,
  cache: { read, write },
})

const assistant = (
  id: string,
  time: SessionMessage["time"],
  usage: TokenUsage,
): SessionMessage => ({
  id,
  type: "assistant",
  time,
  tokens: usage,
})

test("speedSummary averages generation and prefill across steps", () => {
  const speed = speedSummary([
    assistant(
      "a",
      { created: 0, firstToken: 200, streamed: 1_200 },
      tokens(1_000, 100, 50, 500, 0),
    ),
    assistant(
      "b",
      { created: 0, streamed: 2_000 },
      tokens(2_000, 300, 0, 0, 0),
    ),
  ])
  assert.equal(speed.generation, 150)
  assert.equal(speed.prefill, 7_500)
})

test("speedSummary falls back to created when firstToken is missing", () => {
  const speed = speedSummary([
    assistant("a", { created: 1_000, streamed: 3_000 }, tokens(1_000, 400)),
  ])
  assert.equal(speed.generation, 200)
  assert.equal(speed.prefill, undefined)
})

test("speedSummary stops at the revert boundary", () => {
  const speed = speedSummary(
    [
      assistant("a", { created: 0, streamed: 1_000 }, tokens(0, 100)),
      assistant("b", { created: 0, streamed: 1_000 }, tokens(0, 900)),
    ],
    "b",
  )
  assert.equal(speed.generation, 100)
})

test("speedSummary ignores steps without usable timing or tokens", () => {
  assert.deepEqual(speedSummary([]), {
    generation: undefined,
    prefill: undefined,
  })
  assert.deepEqual(
    speedSummary([
      assistant("a", { created: 0, streamed: 0 }, tokens(0, 0)),
      assistant("b", { created: 0 }, tokens(0, 100)),
    ]),
    { generation: undefined, prefill: undefined },
  )
})
