import assert from "node:assert/strict"
import test from "node:test"
import plugin from "../index.js"
import { createGetSessionModelTool } from "../get-session-model.js"

test("reads the calling session's model and variant without switching", async () => {
  const ctx = {
    session: {
      get: async (input) => {
        assert.deepEqual(input, { sessionID: "ses_1" })
        return { model: { providerID: "p", id: "m", variant: "high" } }
      },
      switchModel: () => assert.fail("getter must not switch models"),
    },
  }
  const result = await createGetSessionModelTool(ctx).execute({}, { sessionID: "ses_1" })
  assert.match(result.content, /Model: p\/m\nSelected variant: high/)
})

test("handles wrapped API results and distinguishes default from effective effort", async () => {
  const ctx = { session: { get: async () => ({ data: { model: { providerID: "p", id: "m" } } }) } }
  const result = await createGetSessionModelTool(ctx).execute({}, { sessionID: "ses_1" })
  assert.match(result.content, /Selected variant: default \(no explicit variant selected\)/)
  assert.match(result.content, /not the effective provider reasoning effort/)
})

test("reports missing selection as unknown and propagates API failures", async () => {
  const ctx = { session: { get: async () => ({}) } }
  const tool = createGetSessionModelTool(ctx)
  assert.match((await tool.execute({}, { sessionID: "ses_1" })).content, /unknown/)
  ctx.session.get = async () => { throw new Error("API unavailable") }
  await assert.rejects(tool.execute({}, { sessionID: "ses_1" }), /API unavailable/)
})

test("registers the getter alongside the switching tool", async () => {
  const names = []
  await plugin.setup({ tool: { transform: async (register) => register({ add: (tool) => names.push(tool.name) }) } })
  assert.deepEqual(names, ["switch_model", "get_session_model"])
})
