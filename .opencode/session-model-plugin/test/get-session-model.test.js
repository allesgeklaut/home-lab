import assert from "node:assert/strict"
import test from "node:test"
import plugin from "../index.js"
import { createGetSessionModelTool } from "../get-session-model.js"

test("reads a concrete selection without a catalog read", async () => {
  const ctx = {
    session: {
      get: async (input) => {
        assert.deepEqual(input, { sessionID: "ses_1" })
        return { model: { providerID: "p", id: "m", variant: "high" } }
      },
      switchModel: () => assert.fail("getter must not switch models"),
    },
    model: { list: () => assert.fail("concrete selections need no catalog read") },
  }
  const result = await createGetSessionModelTool(ctx).execute({}, { sessionID: "ses_1" })
  assert.match(result.content, /Model: p\/m\nSelected variant: high/)
  assert.doesNotMatch(result.content, /Provider model:/)
})

test("handles wrapped API results and distinguishes default from effective effort", async () => {
  const ctx = { session: { get: async () => ({ data: { model: { providerID: "p", id: "m" } } }) } }
  const result = await createGetSessionModelTool(ctx).execute({}, { sessionID: "ses_1" })
  assert.match(result.content, /Selected variant: default \(no explicit variant selected\)/)
  assert.match(result.content, /not the effective provider reasoning effort/)
})

test("labels the default variant sentinel as no explicit variant", async () => {
  for (const model of [
    { providerID: "mistral", id: "auto", variant: "default" },
    { providerID: "p", id: "m", variant: "default" },
  ]) {
    const ctx = {
      session: { get: async () => ({ model }) },
      model: { list: async () => [{ providerID: "mistral", id: "auto", modelID: "zai-glm-5-3-flash" }] },
    }
    const result = await createGetSessionModelTool(ctx).execute({}, { sessionID: "ses_1" })
    assert.match(result.content, /Selected variant: default \(no explicit variant selected\)/)
    assert.match(result.content, /not the effective provider reasoning effort/)
  }
})

test("reports missing selection as unknown and propagates API failures", async () => {
  const ctx = { session: { get: async () => ({}) } }
  const tool = createGetSessionModelTool(ctx)
  assert.match((await tool.execute({}, { sessionID: "ses_1" })).content, /unknown/)
  ctx.session.get = async () => { throw new Error("API unavailable") }
  await assert.rejects(tool.execute({}, { sessionID: "ses_1" }), /API unavailable/)
})

test("reports the provider model behind the Auto alias", async () => {
  const ctx = {
    session: { get: async () => ({ data: { model: { providerID: "mistral", id: "auto" } } }) },
    model: { list: async () => ({ data: [
      { providerID: "mistral", id: "auto", modelID: "zai-glm-5-3-flash" },
    ] }) },
  }
  const result = await createGetSessionModelTool(ctx).execute({}, { sessionID: "ses_1" })
  assert.match(result.content, /Model: mistral\/auto/)
  assert.match(result.content, /Provider model: zai-glm-5-3-flash/)
  assert.match(result.content, /Selected variant: default \(no explicit variant selected\)/)
  assert.match(result.content, /not the effective provider reasoning effort/)
})

test("omits the provider model when the alias ID is already the provider ID", async () => {
  const ctx = {
    session: { get: async () => ({ model: { providerID: "mistral", id: "auto" } }) },
    model: { list: async () => [{ providerID: "mistral", id: "auto", modelID: "auto" }] },
  }
  const result = await createGetSessionModelTool(ctx).execute({}, { sessionID: "ses_1" })
  assert.match(result.content, /Model: mistral\/auto/)
  assert.doesNotMatch(result.content, /Provider model:/)
})

test("keeps reporting the selected Auto model when the catalog lookup fails", async () => {
  const cases = [
    { list: async () => { throw new Error("catalog unavailable") }, provider: undefined },
    { list: async () => ({ data: null }), provider: undefined },
    { list: async () => ({ data: { data: [{ providerID: "mistral", id: "auto", modelID: "zai-glm-5-3-flash" }] } }), provider: "zai-glm-5-3-flash" },
    { list: async () => ({ data: [{ providerID: "other", id: "auto", modelID: "other-model" }] }), provider: undefined },
    { list: async () => "not a catalog", provider: undefined },
  ]
  for (const { list, provider } of cases) {
    const ctx = {
      session: { get: async () => ({ model: { providerID: "mistral", id: "auto", variant: "high" } }) },
      model: { list },
    }
    const result = await createGetSessionModelTool(ctx).execute({}, { sessionID: "ses_1" })
    assert.match(result.content, /Model: mistral\/auto/)
    assert.match(result.content, /Selected variant: high/)
    if (provider) assert.match(result.content, new RegExp(`Provider model: ${provider}`))
    else assert.doesNotMatch(result.content, /Provider model:/)
  }

  const withoutCatalog = {
    session: { get: async () => ({ model: { providerID: "mistral", id: "auto" } }) },
  }
  const result = await createGetSessionModelTool(withoutCatalog).execute({}, { sessionID: "ses_1" })
  assert.match(result.content, /Model: mistral\/auto/)
})

test("registers the getter alongside the switching tool", async () => {
  const names = []
  await plugin.setup({ tool: { transform: async (register) => register({ add: (tool) => names.push(tool.name) }) } })
  assert.deepEqual(names, ["switch_model", "get_session_model"])
})
