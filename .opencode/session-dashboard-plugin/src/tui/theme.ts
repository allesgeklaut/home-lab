import type { ColorInput } from "@opentui/core"
import { palette } from "../shared/design.js"

export type Colors = {
  text: ColorInput
  muted: ColorInput
  faint: ColorInput
  line: ColorInput
  accent: ColorInput
  warning: ColorInput
  danger: ColorInput
  panel: ColorInput
}

/** The dashboard's fixed phosphor-green matrix palette. */
export function colorsFor(): Colors {
  return {
    text: palette.dark.text,
    muted: palette.dark.muted,
    faint: palette.dark.muted,
    line: palette.dark.line,
    accent: palette.dark.accent,
    warning: palette.dark.warning,
    danger: palette.dark.danger,
    panel: palette.dark.panel,
  }
}

export function toneColor(colors: Colors, tone: "accent" | "warning" | "danger" | "muted"): ColorInput {
  if (tone === "warning") return colors.warning
  if (tone === "danger") return colors.danger
  if (tone === "muted") return colors.muted
  return colors.accent
}
