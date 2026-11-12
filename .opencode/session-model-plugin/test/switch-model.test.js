import assert from "node:assert/strict"
import test from "node:test"
import { createSwitchModelTool, parseModelRef } from "../switch-model.js"

const models = [
  { providerID: "p", id: "m", variants: [{ id: "low" }, { id: "high" }] },
  { providerID: "p", id: "other", variants: [] },
]

function setup(options = {}, current = { providerID: "p", id: "m", variant: "high" }) {
  const switched = []
  const ctx = {
    options,
    model: { list: async () => ({ data: models }) },
    session: {
      get: async () => ({ model: current }),
      switchModel: async (input) => void switched.push(input),
    },
  }
  const run = (input, agent = "alloy") =>
    createSwitchModelTool(ctx).execute({ reason: "test", ...input }, { agent, sessionID: "ses_1" })
  return { run, switched }
}

test("parses provider/model with an optional variant", () => {
  assert.deepEqual(parseModelRef("a/b/c#max"), { providerID: "a", id: "b/c", variant: "max" })
  assert.deepEqual(parseModelRef("a/b"), { providerID: "a", id: "b", variant: undefined })
  assert.equal(parseModelRef("nomodel"), undefined)
})

test("switches the session model for an enabled agent", async () => {
  const { run, switched } = setup()
  const result = await run({ model: "p/m#low" })
  assert.deepEqual(switched, [{ sessionID: "ses_1", model: { providerID: "p", id: "m", variant: "low" } }])
  assert.match(result.content, /p\/m#high to p\/m#low/)
})

test("does nothing when the session already uses the model", async () => {
  const { run, switched } = setup()
  await run({ model: "p/m#high" })
  assert.equal(switched.length, 0)
})

test("rejects restricted agents, unknown models and variants, and disallowed models", async () => {
  await assert.rejects(setup({ agents: ["alloy"] }).run({ model: "p/m" }, "build"), /not enabled for the build agent/)
  await assert.rejects(setup().run({ model: "p/m" }, "reviewer"), /authorized Auto handoff/)
  await assert.rejects(setup().run({ model: "p/missing" }), /not available/)
  await assert.rejects(setup().run({ model: "p/m#ultra" }), /Unknown variant "ultra".*low, high/)
  await assert.rejects(setup({ models: ["p/other"] }).run({ model: "p/m" }), /not allowed/)
})

test("allows manual switching for Build, Plan and custom agents by default", async () => {
  for (const agent of ["build", "plan", "custom", "alloy"]) {
    const { run, switched } = setup()
    await run({ model: "p/other" }, agent)
    assert.equal(switched.length, 1)
  }
})

test("treats the default variant sentinel as omitted for no-op switches", async () => {
  const { run, switched } = setup({}, { providerID: "p", id: "m", variant: "default" })
  await run({ model: "p/m" })
  assert.equal(switched.length, 0)
})

test("refuses every switch when the models option is invalid", async () => {
  const { run, switched } = setup({ models: "p/*" })
  await assert.rejects(run({ model: "p/m" }), /Invalid plugin option "models"/)
  assert.equal(switched.length, 0)
})

test("does not broaden access when the agents option is invalid", async () => {
  const { run, switched } = setup({ agents: "alloy" })
  await assert.rejects(run({ model: "p/m" }, "build"), /Invalid plugin option "agents"/)
  assert.equal(switched.length, 0)
})

test("honors configured agents and model patterns", async () => {
  const { run, switched } = setup({ agents: ["build"], models: ["p/*"] })
  await run({ model: "p/other" }, "build")
  assert.equal(switched.length, 1)
})
