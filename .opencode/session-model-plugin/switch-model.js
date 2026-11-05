const DEFAULT_AGENTS = ["alloy"]

/** Splits `provider/model` or `provider/model#variant`, the reference format OpenCode uses everywhere. */
export function parseModelRef(value) {
  const hash = value.indexOf("#")
  const ref = hash === -1 ? value : value.slice(0, hash)
  const variant = hash === -1 ? undefined : value.slice(hash + 1) || undefined
  const slash = ref.indexOf("/")
  if (slash < 1 || slash === ref.length - 1) return undefined
  return { providerID: ref.slice(0, slash), id: ref.slice(slash + 1), variant }
}

const matches = (pattern, value) =>
  new RegExp(`^${pattern.split("*").map((part) => part.replace(/[.+?^${}()|[\]\\]/g, "\\$&")).join(".*")}$`).test(value)

const unwrap = (result) => (result && !Array.isArray(result) && "data" in result ? result.data : result)

const strings = (value, fallback) =>
  Array.isArray(value) && value.every((item) => typeof item === "string") ? value : fallback

export function createSwitchModelTool(ctx) {
  const agents = strings(ctx.options?.agents, DEFAULT_AGENTS)
  // An invalid `models` option must not widen access: it makes every switch fail instead of being ignored.
  const allowed = ctx.options?.models === undefined ? undefined : strings(ctx.options.models, null)

  return {
    name: "switch_model",
    description:
      "Switch the model and optionally its reasoning variant for subsequent requests in this session. " +
      "Switching starts with a cold prompt cache. Omitting the variant selects the target model's default. " +
      "Provide a short reason for the change.",
    input: {
      type: "object",
      properties: {
        model: {
          type: "string",
          description: 'Target as "provider/model" or "provider/model#variant". Omitting the variant selects the default.',
        },
        reason: { type: "string", description: "Short reason for the switch." },
      },
      required: ["model", "reason"],
      additionalProperties: false,
    },
    options: { codemode: false },
    async execute(input, context) {
      if (!agents.includes(context.agent)) throw new Error(`switch_model is not enabled for the ${context.agent} agent.`)

      const ref = parseModelRef(String(input.model ?? ""))
      if (!ref) throw new Error('Invalid model. Use "provider/model" or "provider/model#variant".')

      const name = `${ref.providerID}/${ref.id}`
      if (allowed === null) throw new Error('Invalid plugin option "models": expected an array of "provider/model" patterns.')
      if (allowed && !allowed.some((pattern) => matches(pattern, name)))
        throw new Error(`Model ${name} is not allowed. Allowed: ${allowed.join(", ")}`)

      const models = unwrap(await ctx.model.list())
      const model = models.find((item) => item.providerID === ref.providerID && item.id === ref.id)
      if (!model) throw new Error(`Model not available: ${name}`)

      const variants = (model.variants ?? []).map((item) => item.id ?? item)
      if (ref.variant && !variants.includes(ref.variant))
        throw new Error(`Unknown variant "${ref.variant}" for ${name}. Available: ${variants.join(", ") || "none"}`)

      const current = unwrap(await ctx.session.get({ sessionID: context.sessionID })).model
      const label = (value) => `${value.providerID}/${value.id}${value.variant ? `#${value.variant}` : ""}`
      if (current && current.providerID === ref.providerID && current.id === ref.id && current.variant === ref.variant)
        return { content: `Session already uses ${label(ref)}; nothing changed.` }

      await ctx.session.switchModel({
        sessionID: context.sessionID,
        model: { providerID: ref.providerID, id: ref.id, ...(ref.variant ? { variant: ref.variant } : {}) },
      })
      return {
        content: `Switched session model from ${current ? label(current) : "unknown"} to ${label(ref)} (${input.reason}). It applies to subsequent model requests.`,
      }
    },
  }
}
