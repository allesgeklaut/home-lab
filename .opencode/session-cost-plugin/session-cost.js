const unwrap = (result) => result && !Array.isArray(result) && "data" in result ? result.data : result
const validCost = (cost) => typeof cost === "number" && Number.isFinite(cost) && cost >= 0
const usd = (cost) => `$${cost.toFixed(4)}`

async function children(ctx, sessionID, signal) {
  const found = []
  const ids = new Set()
  const cursors = new Set()
  let cursor

  while (true) {
    const input = cursor ? { cursor, limit: 100 } : { parentID: sessionID, limit: 100 }
    const page = await ctx.session.list(input, { signal })
    const items = unwrap(page)
    if (!Array.isArray(items)) throw new Error("Invalid session list")
    for (const child of items) {
      if (!child || typeof child.id !== "string" || child.parentID !== sessionID || ids.has(child.id)) {
        throw new Error("Invalid child session")
      }
      ids.add(child.id)
      found.push(child)
    }
    const next = page?.cursor?.next
    if (!next) return found
    if (typeof next !== "string" || cursors.has(next)) throw new Error("Invalid session cursor")
    cursors.add(next)
    cursor = next
  }
}

export function createGetSessionCostTool(ctx) {
  return {
    name: "get_session_cost",
    description:
      "Read OpenCode's reported cost for the current session and its immediate child sessions. " +
      "Use for advisory cost-aware decisions; this is not a spending limit or provider invoice.",
    input: { type: "object", properties: {}, additionalProperties: false },
    options: { codemode: false },
    async execute(_input, context) {
      const { sessionID, signal } = context
      const session = unwrap(await ctx.session.get({ sessionID }, { signal }))
      const own = validCost(session?.cost) ? usd(session.cost) : "unavailable"
      const lines = [`Current session: ${own} USD`]

      try {
        const list = await children(ctx, sessionID, signal)
        const known = list.filter((child) => validCost(child.cost))
        const unknown = list.length - known.length
        const sum = known.reduce((total, child) => total + child.cost, 0)
        if (unknown) {
          lines.push(`Immediate child sessions: ${list.length}; known cost ${usd(sum)} USD; ${unknown} cost(s) unavailable`)
        } else {
          lines.push(`Immediate child sessions: ${list.length}; reported cost ${usd(sum)} USD`)
        }
        if (validCost(session?.cost) && !unknown) {
          lines.push(`Current session + immediate children: ${usd(session.cost + sum)} USD`)
        } else {
          lines.push("Current session + immediate children: unavailable (cost data incomplete)")
        }
      } catch (error) {
        if (signal?.aborted) throw error
        lines.push("Immediate child sessions: unavailable (listing failed or was incomplete)")
        lines.push("Current session + immediate children: unavailable (cost data incomplete)")
      }

      lines.push(
        "OpenCode-reported estimates are not a provider invoice or a hard budget; in-flight work may not yet be included. Only immediate children are included, and values are read at slightly different times.",
      )
      return { content: lines.join("\n") }
    },
  }
}
