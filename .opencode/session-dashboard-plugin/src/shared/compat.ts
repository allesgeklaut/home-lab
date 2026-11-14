export const TESTED_WITH = ">=2.3.0 <2.4.0"

export function isTestedVersion(version: string | undefined): boolean {
  if (!version) return false
  const match = version.match(/^(\d+)\.(\d+)(?:\.(\d+))?/)
  if (!match) return false
  const major = Number(match[1])
  const minor = Number(match[2])
  return major === 2 && minor === 3
}

export function hasFunctions(value: unknown, names: ReadonlyArray<string>): boolean {
  if (!value || (typeof value !== "object" && typeof value !== "function")) return false
  return names.every((name) => typeof (value as Record<string, unknown>)[name] === "function")
}

export function missingFunctions(value: unknown, names: ReadonlyArray<string>): string[] {
  if (!value || (typeof value !== "object" && typeof value !== "function")) return [...names]
  return names.filter((name) => typeof (value as Record<string, unknown>)[name] !== "function")
}
