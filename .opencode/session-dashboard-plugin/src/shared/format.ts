import type { ModelInfo, ModelRef } from "./types.js"

export function formatTokens(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0"
  if (value < 1_000) return String(Math.round(value))
  if (value < 1_000_000) return trim(value / 1_000) + "k"
  if (value < 1_000_000_000) return trim(value / 1_000_000) + "M"
  return trim(value / 1_000_000_000) + "B"
}

export function formatSpeed(value: number | undefined): string {
  if (value === undefined || !Number.isFinite(value) || value <= 0) return "—"
  if (value >= 1_000) return trim(value / 1_000) + "k"
  return value.toFixed(1)
}

export function formatCost(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "$0.00"
  if (value < 0.01) return "$" + value.toFixed(4)
  return "$" + value.toFixed(2)
}

export function formatPercent(value: number | undefined, digits = 1): string {
  if (value === undefined || !Number.isFinite(value)) return "—"
  return (value * 100).toFixed(digits).replace(/\.0$/, "") + "%"
}

export function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return "00:00"
  const total = Math.floor(ms / 1000)
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const seconds = total % 60
  if (hours > 0) return `${hours}:${pad(minutes)}:${pad(seconds)}`
  return `${pad(minutes)}:${pad(seconds)}`
}

export function formatAgo(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return "just now"
  if (ms < 5_000) return "just now"
  if (ms < 60_000) return `${Math.floor(ms / 1000)}s ago`
  if (ms < 3_600_000) return `${Math.floor(ms / 60_000)}m ago`
  if (ms < 86_400_000) return `${Math.floor(ms / 3_600_000)}h ago`
  return `${Math.floor(ms / 86_400_000)}d ago`
}

export function formatModel(ref: ModelRef | undefined, model?: ModelInfo): string {
  if (!ref) return "unknown"
  const variant = ref.variant ? `#${ref.variant}` : ""
  const alias = model && model.modelID && model.modelID !== model.id ? ` → ${model.modelID}` : ""
  return `${ref.providerID}/${ref.id}${variant}${alias}`
}

export function formatModelShort(ref: ModelRef | undefined, model?: ModelInfo): string {
  if (!ref) return "unknown"
  const variant = ref.variant ? `#${ref.variant}` : ""
  const id = model?.modelID && model.modelID !== model.id ? model.modelID : ref.id
  return `${id}${variant}`
}

export function formatCount(value: number): string {
  return Number.isFinite(value) ? String(Math.round(value)) : "0"
}

function trim(value: number): string {
  return value.toFixed(1).replace(/\.0$/, "")
}

function pad(value: number): string {
  return String(value).padStart(2, "0")
}
