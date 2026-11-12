import { markerKey, pendingHandoff, sameSelection } from "./routing-handoff.js"

const DEFAULT_AGENTS = ["*"]

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

// Shared by explicit tool switches and automatic routing; neither may bypass the model policy.
export async function validateModelRef(ctx, ref, available) {
  const allowed = ctx.options?.models === undefined ? undefined : strings(ctx.options.models, null)
  const name = `${ref.providerID}/${ref.id}`
  if (allowed === null) throw new Error('Invalid plugin option "models": expected an array of "provider/model" patterns.')
  if (allowed && !allowed.some((pattern) => matches(pattern, name)))
    throw new Error(`Model ${name} is not allowed. Allowed: ${allowed.join(", ")}`)

  const models = available ?? unwrap(await ctx.model.list())
  const model = models.find((item) => item.providerID === ref.providerID && item.id === ref.id && item.enabled !== false)
  if (!model) throw new Error(`Model not available: ${name}`)
  const variants = (model.variants ?? []).map((item) => item.id ?? item)
  if (ref.variant && !variants.includes(ref.variant))
    throw new Error(`Unknown variant "${ref.variant}" for ${name}. Available: ${variants.join(", ") || "none"}`)
}

export function createSwitchModelTool(ctx) {
  const agents = ctx.options?.agents === undefined ? DEFAULT_AGENTS : strings(ctx.options.agents, null)
  const active = new Set()

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
        routing: { type: "string", description: "Auto handoff token supplied by the routing instruction. Omit for manual switching." },
      },
      required: ["model", "reason"],
      additionalProperties: false,
    },
    options: { codemode: false },
    async execute(input, context) {
      if (active.has(context.sessionID)) throw new Error("A session model switch is already in progress.")
      active.add(context.sessionID)
      // Reviewer is never granted unrestricted switching, even with agents: ["*"].
      let handoff
      try {
        if (agents === null) throw new Error('Invalid plugin option "agents": expected an array of agent ID patterns.')
        if (input.routing !== undefined) {
          handoff = await pendingHandoff(ctx, context.sessionID, context.agent, input.routing)
          if (!handoff || handoff.model !== input.model) throw new Error("Auto handoff is no longer valid for this prompt, agent, selection or target.")
        } else if (context.agent === "reviewer" || !agents.some((pattern) => matches(pattern, context.agent))) {
          throw new Error(`switch_model is not enabled for the ${context.agent} agent without an authorized Auto handoff.`)
        }

        const ref = parseModelRef(String(input.model ?? ""))
        if (!ref) throw new Error('Invalid model. Use "provider/model" or "provider/model#variant".')
        await validateModelRef(ctx, ref)
        const current = unwrap(await ctx.session.get({ sessionID: context.sessionID })).model
        const label = (value) => `${value.providerID}/${value.id}${value.variant ? `#${value.variant}` : ""}`
        if (!handoff && sameSelection(current, ref))
          return { content: `Session already uses ${label(ref)}; nothing changed.` }

        if (handoff) {
          // Validation may await a refreshed catalog. Re-check the grant at the
          // last practical boundary before dispatch; the server has no CAS API.
          const latest = await pendingHandoff(ctx, context.sessionID, context.agent, input.routing)
          if (!latest || latest.model !== input.model) throw new Error("Auto handoff changed before switching.")
          await ctx.storage.set(markerKey(context.sessionID, handoff.epoch), { ...handoff, status: "dispatching" })
        }
        await ctx.session.switchModel({
          sessionID: context.sessionID,
          model: { providerID: ref.providerID, id: ref.id, ...(ref.variant ? { variant: ref.variant } : {}) },
        })
        if (handoff) {
          try {
            await ctx.storage.set(markerKey(context.sessionID, handoff.epoch), { ...handoff, status: "switched" })
          } catch {
            // The concrete model already invalidates the handoff even if saving fails.
          }
        }
        return {
          content: `Switched session model from ${current ? label(current) : "unknown"} to ${label(ref)} (${input.reason}). It applies to subsequent model requests.`,
        }
      } catch (error) {
        if (handoff) {
          try {
            await ctx.storage.set(markerKey(context.sessionID, handoff.epoch), { ...handoff, status: "failed" })
          } catch {
            // Never mask the tool error with a bookkeeping failure.
          }
        }
        throw error
      } finally {
        active.delete(context.sessionID)
      }
    },
  }
}
