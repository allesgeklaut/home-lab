const unwrap = (result) => result && !Array.isArray(result) && "data" in result ? result.data : result
const AUTO_PROVIDER = "mistral"
const AUTO_MODEL = "auto"

// The alias's provider-facing model ID can differ from the selected alias ID.
// Read it only for Auto so ordinary concrete selections need no catalog call.
async function providerModelID(ctx, model) {
  try {
    if (typeof ctx.model?.list !== "function") return undefined
    let models = unwrap(await ctx.model.list())
    if (!Array.isArray(models)) models = unwrap(models)
    if (!Array.isArray(models)) return undefined
    const entry = models.find((item) => item?.providerID === model.providerID && item?.id === model.id)
    return typeof entry?.modelID === "string" && entry.modelID ? entry.modelID : undefined
  } catch {
    // A catalog failure must not hide the selected model.
    return undefined
  }
}

export function createGetSessionModelTool(ctx) {
  return {
    name: "get_session_model",
    description:
      "Read the current session's selected model and reasoning variant without changing anything. " +
      "An omitted variant means default selection, not a verified effective provider reasoning effort.",
    input: { type: "object", properties: {}, additionalProperties: false },
    options: { codemode: false },
    async execute(input, context) {
      const result = await ctx.session.get({ sessionID: context.sessionID })
      const session = unwrap(result)
      const model = session?.model
      if (!model) return { content: "Current session model: unknown (not exposed by the session API)." }

      const lines = [`Model: ${model.providerID}/${model.id}`]
      if (model.providerID === AUTO_PROVIDER && model.id === AUTO_MODEL) {
        const providerID = await providerModelID(ctx, model)
        if (providerID && providerID !== model.id) lines.push(`Provider model: ${providerID}`)
      }
      // "default" is the session view's sentinel for an omitted variant, not a
      // verified provider effort; present both forms the same way.
      const variant = model.variant && model.variant !== "default" ? model.variant : undefined
      lines.push(
        `Selected variant: ${variant || "default (no explicit variant selected)"}`,
        "This reports the session selection, not the effective provider reasoning effort.",
      )
      return { content: lines.join("\n") }
    },
  }
}
