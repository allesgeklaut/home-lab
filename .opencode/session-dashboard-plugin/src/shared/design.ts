export const BAR_CELLS = 24
export const WARN_AT = 0.75
export const DANGER_AT = 0.9

export const palette = {
  dark: {
    bg: "#060A08",
    panel: "#0A110E",
    line: "#1A2A22",
    text: "#D8E8DF",
    muted: "#8FA79A",
    accent: "#6FF3B0",
    accentDim: "#2C7A57",
    warning: "#FFC46B",
    danger: "#FF6B5E",
    info: "#6BD5FF",
  },
  light: {
    bg: "#F3F1E8",
    panel: "#FBFAF3",
    line: "#D5D2C4",
    text: "#1B241F",
    muted: "#6B7A70",
    accent: "#0E7A4F",
    accentDim: "#7FBFA2",
    warning: "#A85B00",
    danger: "#B3261E",
    info: "#006A8A",
  },
} as const

export type Tone = "accent" | "warning" | "danger" | "muted"

export function toneFor(ratio: number | undefined): Tone {
  if (ratio === undefined || !Number.isFinite(ratio)) return "muted"
  if (ratio >= DANGER_AT) return "danger"
  if (ratio >= WARN_AT) return "warning"
  return "accent"
}

export function barSegments(ratio: number | undefined, cells = BAR_CELLS) {
  if (ratio === undefined || !Number.isFinite(ratio)) {
    return { filled: 0, empty: cells, percent: undefined as number | undefined }
  }
  const clamped = Math.max(0, Math.min(1, ratio))
  const filled = Math.round(clamped * cells)
  return {
    filled,
    empty: Math.max(0, cells - filled),
    percent: clamped,
  }
}
