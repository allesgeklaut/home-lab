import type { TokenUsage, UsageSummary } from "./types.js"

export const emptyTokens = (): TokenUsage => ({
  input: 0,
  output: 0,
  reasoning: 0,
  cache: { read: 0, write: 0 },
})

export function normalizeTokens(tokens: TokenUsage | undefined): TokenUsage {
  if (!tokens) return emptyTokens()
  return {
    input: number(tokens.input),
    output: number(tokens.output),
    reasoning: number(tokens.reasoning),
    cache: {
      read: number(tokens.cache?.read),
      write: number(tokens.cache?.write),
    },
  }
}

export function promptTokens(tokens: TokenUsage): number {
  return tokens.input + tokens.cache.read + tokens.cache.write
}

export function totalTokens(tokens: TokenUsage): number {
  return promptTokens(tokens) + tokens.output + tokens.reasoning
}

export function cacheHitRate(tokens: TokenUsage): number | undefined {
  const prompt = promptTokens(tokens)
  return prompt > 0 ? tokens.cache.read / prompt : undefined
}

export function addTokens(a: TokenUsage, b: TokenUsage): TokenUsage {
  return {
    input: a.input + b.input,
    output: a.output + b.output,
    reasoning: a.reasoning + b.reasoning,
    cache: {
      read: a.cache.read + b.cache.read,
      write: a.cache.write + b.cache.write,
    },
  }
}

export function summarizeUsage(tokens: TokenUsage, cost = 0): UsageSummary {
  const normalized = normalizeTokens(tokens)
  return {
    input: normalized.input,
    output: normalized.output,
    reasoning: normalized.reasoning,
    cacheRead: normalized.cache.read,
    cacheWrite: normalized.cache.write,
    prompt: promptTokens(normalized),
    total: totalTokens(normalized),
    cacheHitRate: cacheHitRate(normalized),
    cost: number(cost),
  }
}

function number(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0
}
