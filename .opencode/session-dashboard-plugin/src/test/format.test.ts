import assert from "node:assert/strict"
import test from "node:test"
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

test("formatTokens uses compact units", () => {
  assert.equal(formatTokens(0), "0")
  assert.equal(formatTokens(999), "999")
  assert.equal(formatTokens(1_000), "1k")
  assert.equal(formatTokens(1_500), "1.5k")
  assert.equal(formatTokens(1_000_000), "1M")
})

test("formatSpeed keeps one decimal and compacts thousands", () => {
  assert.equal(formatSpeed(156.618), "156.6")
  assert.equal(formatSpeed(9.94), "9.9")
  assert.equal(formatSpeed(1_500), "1.5k")
  assert.equal(formatSpeed(undefined), "—")
  assert.equal(formatSpeed(0), "—")
})

test("formatCost keeps small costs visible", () => {
  assert.equal(formatCost(0), "$0.00")
  assert.equal(formatCost(0.004), "$0.0040")
  assert.equal(formatCost(0.42), "$0.42")
})

test("formatPercent handles missing values", () => {
  assert.equal(formatPercent(0.928), "92.8%")
  assert.equal(formatPercent(0.5), "50%")
  assert.equal(formatPercent(undefined), "—")
})

test("formatDuration renders mm:ss and h:mm:ss", () => {
  assert.equal(formatDuration(134_000), "02:14")
  assert.equal(formatDuration(3_600_000), "1:00:00")
})

test("formatAgo renders relative time", () => {
  assert.equal(formatAgo(0), "just now")
  assert.equal(formatAgo(12_000), "12s ago")
  assert.equal(formatAgo(180_000), "3m ago")
})

test("formatModel shows aliases and variants", () => {
  const model = {
    id: "auto",
    modelID: "deepseek-v4-1-flash-1017",
    providerID: "mistral",
    name: "Auto",
    limit: { context: 1_000, output: 100 },
  }
  assert.equal(
    formatModel({ providerID: "mistral", id: "auto", variant: "max" }, model),
    "mistral/auto#max → deepseek-v4-1-flash-1017",
  )
  assert.equal(
    formatModelShort({ providerID: "mistral", id: "auto", variant: "max" }, model),
    "deepseek-v4-1-flash-1017#max",
  )
})
