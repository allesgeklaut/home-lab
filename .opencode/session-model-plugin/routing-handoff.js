const unwrap = (result) => result && !Array.isArray(result) && "data" in result ? result.data : result
export const isAuto = (model) => model?.providerID === "mistral" && model?.id === "auto"
export const markerKey = (sessionID, epoch) => `routing/v2/${sessionID}/${epoch}`
export const sameSelection = (left, right) => !!left && !!right &&
  left.providerID === right.providerID && left.id === right.id &&
  (left.variant ?? "default") === (right.variant ?? "default")

// Resolve an unstored selection using the same precedence as the runner.
export async function effectiveModel(ctx, session, wait = (run) => run()) {
  if (session.model) return session.model
  if (session.agent) {
    try {
      const agent = unwrap(await wait(() => ctx.agent.get({ agentID: session.agent })))
      if (agent?.model) {
        let models
        try { models = unwrap(await wait(() => ctx.model.list())) }
        catch { return agent.model }
        if (!Array.isArray(models)) return agent.model
        if (models.some((item) => item?.providerID === agent.model.providerID &&
            item?.id === agent.model.id && item.enabled !== false)) return agent.model
      }
    } catch {
      // An unreadable agent preference falls through to the default.
    }
  }
  return unwrap(await wait(() => ctx.model.default())) ?? undefined
}

// Only an admitted, latest user prompt can authorize a handoff. Metadata is
// checked against plugin-owned storage, not trusted as an authorization source.
export async function pendingHandoff(ctx, sessionID, agent, routing) {
  if (ctx.options?.routing?.enabled === false || !ctx.options?.routing) return
  const [sessionResult, historyResult] = await Promise.all([
    ctx.session.get({ sessionID }), ctx.session.context({ sessionID }),
  ])
  const session = unwrap(sessionResult)
  const history = unwrap(historyResult)
  if (session.agent !== agent || !Array.isArray(history)) return
  const user = history.findLast((item) => item?.type === "user")
  const metadata = (user?.payload ?? user)?.metadata?.sessionModelRouting
  const epoch = routing ?? metadata?.epoch
  if (typeof epoch !== "string" || !epoch || metadata?.epoch !== epoch) return
  const record = await ctx.storage.get(markerKey(sessionID, epoch))
  if (record?.status !== "picked" || record.messageID !== user?.id ||
      record.agent !== agent || metadata?.status !== "picked" || metadata?.model !== record.model) return
  const latest = history.findLast((item) => item?.type === "model-switched")
  if (latest ? latest.id !== epoch || !isAuto(latest.model) : epoch !== "initial") return
  if (!sameSelection(await effectiveModel(ctx, session), record.source)) return
  // A changed shortlist or policy must not turn an old instruction into a grant.
  if (!ctx.options.routing.candidates?.some((item) => item.model === record.model)) return
  return { ...record, epoch }
}

export function createRoutingInstruction(ctx) {
  return async (event) => {
    if (!isAuto(event.model) || !event.tools?.switch_model) return
    try {
      let timer
      const record = await Promise.race([
        pendingHandoff(ctx, event.sessionID, event.agent),
        new Promise((resolve) => { timer = setTimeout(resolve, 2000) }),
      ]).finally(() => clearTimeout(timer))
      if (!record) return
      event.messages.push({ role: "user", content: [{ type: "text", text: [
        "<auto-routing-handoff>",
        `CURRENT pending Auto selection: ${record.epoch}. Current session selection is mistral/auto.`,
        "This is a NEW handoff for the current prompt. Earlier switch_model calls in the transcript do NOT satisfy it.",
        "The prompt hook has picked a model but has NOT switched this Auto selection.",
        "Before inspecting files, using other tools, or answering the task, call switch_model alone with:",
        JSON.stringify({ model: record.model, reason: "Apply Auto routing selection", routing: record.epoch }),
        "After the tool succeeds, continue the user's task on the next model request. Do not answer the task in this request.",
        "If the handoff is rejected or fails, do not retry or choose another model; continue on the current selection and report the constraint.",
        "</auto-routing-handoff>",
      ].join("\n") }] })
    } catch {
      // Routing infrastructure must not block the task on the fallback model.
    }
  }
}
