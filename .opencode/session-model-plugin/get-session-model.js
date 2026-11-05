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
      const session = result && "data" in result ? result.data : result
      const model = session?.model
      if (!model) return { content: "Current session model: unknown (not exposed by the session API)." }

      return {
        content:
          `Model: ${model.providerID}/${model.id}\n` +
          `Selected variant: ${model.variant || "default (no explicit variant selected)"}\n` +
          "This reports the session selection, not the effective provider reasoning effort.",
      }
    },
  }
}
