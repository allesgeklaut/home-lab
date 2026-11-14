import assert from "node:assert/strict"
import test from "node:test"
import {
  addTokens,
  cacheHitRate,
  normalizeTokens,
  promptTokens,
  summarizeUsage,
  totalTokens,
} from "../shared/usage.js"
import type { TokenUsage } from "../shared/types.js"

const tokens = (
  input: number,
  output: number,
  reasoning: number,
  read: number,
  write: number,
): TokenUsage => ({ input, output, reasoning, cache: { read, write } })

test("promptTokens sums uncached input and both cache sides", () => {
  assert.equal(promptTokens(tokens(100, 20, 5, 300, 40)), 440)
})

test("totalTokens includes output and reasoning", () => {
  assert.equal(totalTokens(tokens(100, 20, 5, 300, 40)), 465)
})

test("cacheHitRate uses cache reads over the whole prompt", () => {
  assert.equal(cacheHitRate(tokens(100, 20, 5, 300, 40)), 300 / 440)
  assert.equal(cacheHitRate(tokens(0, 0, 0, 0, 0)), undefined)
})

test("addTokens adds every category", () => {
  const result = addTokens(tokens(1, 2, 3, 4, 5), tokens(10, 20, 30, 40, 50))
  assert.deepEqual(result, tokens(11, 22, 33, 44, 55))
})

test("normalizeTokens tolerates missing values", () => {
  assert.deepEqual(normalizeTokens(undefined), tokens(0, 0, 0, 0, 0))
})

test("summarizeUsage exposes prompt, total, hit rate and cost", () => {
  const summary = summarizeUsage(tokens(100, 20, 5, 300, 40), 0.42)
  assert.equal(summary.prompt, 440)
  assert.equal(summary.total, 465)
  assert.equal(summary.cacheRead, 300)
  assert.equal(summary.cacheWrite, 40)
  assert.equal(summary.cacheHitRate, 300 / 440)
  assert.equal(summary.cost, 0.42)
})
