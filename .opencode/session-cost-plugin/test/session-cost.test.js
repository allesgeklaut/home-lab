import assert from "node:assert/strict"
import test from "node:test"
import plugin from "../index.js"
import { createGetSessionCostTool } from "../session-cost.js"

test("reports the current session and paginated immediate children separately", async () => {
  const calls = []
  const ctx = { session: {
    get: async (input) => {
      assert.deepEqual(input, { sessionID: "ses_parent" })
      return { data: { cost: 0.72 } }
    },
    list: async (input) => {
      calls.push(input)
      if (!input.cursor) return { data: [{ id: "ses_a", parentID: "ses_parent", cost: 0.05 }], cursor: { next: "next" } }
      return { data: [{ id: "ses_b", parentID: "ses_parent", cost: 0.04 }], cursor: {} }
    },
  } }
  const result = await createGetSessionCostTool(ctx).execute({}, { sessionID: "ses_parent" })
  assert.match(result.content, /Current session: \$0\.7200 USD/)
  assert.match(result.content, /Immediate child sessions: 2; reported cost \$0\.0900 USD/)
  assert.match(result.content, /Current session \+ immediate children: \$0\.8100 USD/)
  assert.deepEqual(calls, [{ parentID: "ses_parent", limit: 100 }, { cursor: "next", limit: 100 }])
})

test("follows a short page cursor through the empty final page", async () => {
  const ctx = { session: {
    get: async () => ({ cost: 0 }),
    list: async (input) => input.cursor
      ? { data: [], cursor: {} }
      : { data: [{ id: "ses_a", parentID: "ses_parent", cost: 0.02 }], cursor: { next: "end" } },
  } }
  const result = await createGetSessionCostTool(ctx).execute({}, { sessionID: "ses_parent" })
  assert.match(result.content, /Current session: \$0\.0000 USD/)
  assert.match(result.content, /Immediate child sessions: 1; reported cost \$0\.0200 USD/)
})

test("continues after an empty page that has a next cursor", async () => {
  const calls = []
  const ctx = { session: {
    get: async () => ({ cost: 1 }),
    list: async (input) => {
      calls.push(input)
      return input.cursor
        ? { data: [{ id: "ses_child", parentID: "ses_parent", cost: 2 }], cursor: {} }
        : { data: [], cursor: { next: "next" } }
    },
  } }
  const result = await createGetSessionCostTool(ctx).execute({}, { sessionID: "ses_parent" })
  assert.equal(calls.length, 2)
  assert.match(result.content, /Current session \+ immediate children: \$3\.0000 USD/)
})

test("does not report a total when empty pages repeat a cursor", async () => {
  const ctx = { session: {
    get: async () => ({ cost: 1 }),
    list: async () => ({ data: [], cursor: { next: "same" } }),
  } }
  const result = await createGetSessionCostTool(ctx).execute({}, { sessionID: "ses_parent" })
  assert.match(result.content, /Current session \+ immediate children: unavailable/)
})

test("distinguishes missing costs from zero and warns on incomplete data", async () => {
  const ctx = { session: {
    get: async () => ({}),
    list: async () => ({ data: [
      { id: "ses_a", parentID: "ses_parent", cost: 0 },
      { id: "ses_b", parentID: "ses_parent" },
    ], cursor: {} }),
  } }
  const result = await createGetSessionCostTool(ctx).execute({}, { sessionID: "ses_parent" })
  assert.match(result.content, /Current session: unavailable/)
  assert.match(result.content, /known cost \$0\.0000 USD; 1 cost\(s\) unavailable/)
  assert.match(result.content, /Current session \+ immediate children: unavailable/)
})

test("does not report a misleading total when child listing fails or repeats a cursor", async () => {
  for (const list of [
    async () => { throw new Error("private backend details") },
    async (input) => ({ data: [{ id: input.cursor ? "ses_b" : "ses_a", parentID: "ses_parent", cost: 1 }], cursor: { next: "same" } }),
  ]) {
    const ctx = { session: { get: async () => ({ cost: 1 }), list } }
    const result = await createGetSessionCostTool(ctx).execute({}, { sessionID: "ses_parent" })
    assert.match(result.content, /listing failed or was incomplete/)
    assert.match(result.content, /Current session \+ immediate children: unavailable/)
    assert.doesNotMatch(result.content, /private backend details/)
  }
})

test("rejects out-of-scope or duplicate child records", async () => {
  for (const data of [
    [{ id: "ses_a", parentID: "ses_other", cost: 1 }],
    [{ id: "ses_a", parentID: "ses_parent", cost: 1 }, { id: "ses_a", parentID: "ses_parent", cost: 1 }],
  ]) {
    const ctx = { session: { get: async () => ({ cost: 1 }), list: async () => ({ data, cursor: {} }) } }
    const result = await createGetSessionCostTool(ctx).execute({}, { sessionID: "ses_parent" })
    assert.match(result.content, /listing failed or was incomplete/)
  }
})

test("registers the read-only tool", async () => {
  const tools = []
  await plugin.setup({ tool: { transform: async (register) => register({ add: (tool) => tools.push(tool) }) } })
  assert.equal(tools.length, 1)
  assert.equal(tools[0].name, "get_session_cost")
})
