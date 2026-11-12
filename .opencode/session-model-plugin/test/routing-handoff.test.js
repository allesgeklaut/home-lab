import assert from "node:assert/strict"
import test from "node:test"
import { createFirstPromptRouter } from "../first-prompt-router.js"
import { createRoutingInstruction, markerKey } from "../routing-handoff.js"
import { createSwitchModelTool } from "../switch-model.js"

const AUTO = { providerID: "mistral", id: "auto", variant: "default" }
const target = "mistral/small#high"

async function setup(agent = "build", initialModel = AUTO, history = []) {
  const records = new Map()
  const state = { session: { agent, model: initialModel }, history, switched: [] }
  const ctx = {
    options: { agents: ["*"], models: ["mistral/*"], routing: {
      selector: "mistral/large", candidates: [{ model: target, description: "Small" }], timeoutMs: 1000,
    } },
    storage: { get: async (key) => records.get(key), set: async (key, value) => { records.set(key, value) } },
    agent: { get: async () => ({ model: AUTO }) },
    model: { default: async () => AUTO, list: async () => [
      { providerID: "mistral", id: "auto" }, { providerID: "mistral", id: "large" },
      { providerID: "mistral", id: "small", variants: ["high"] },
    ] },
    session: {
      get: async () => state.session,
      context: async () => state.history,
      switchModel: async (input) => {
        state.switched.push(input)
        state.session = { ...state.session, model: input.model }
        state.history.push({ id: "msg_concrete", type: "model-switched", model: input.model })
      },
    },
    experimental: { generate: { text: async () => ({ text: JSON.stringify({ model: target, reason: "Enough" }) }) } },
  }
  const prompt = { sessionID: "ses_test", messageID: "msg_user", prompt: { text: "Test task", files: [{ uri: "file:///test", mention: { start: 0, end: 4 } }] } }
  await createFirstPromptRouter(ctx)(prompt)
  const admit = () => state.history.push({ id: prompt.messageID, type: "user", text: prompt.prompt.text, metadata: prompt.metadata })
  const event = () => ({ sessionID: "ses_test", agent, model: state.session.model ?? AUTO, system: [], messages: [], tools: { switch_model: {} } })
  const instruction = createRoutingInstruction(ctx)
  const tool = createSwitchModelTool(ctx)
  const run = (input = {}) => tool.execute({ model: target, reason: "Test handoff", routing: prompt.metadata?.sessionModelRouting?.epoch, ...input }, { sessionID: "ses_test", agent })
  return { ctx, state, records, prompt, admit, event, instruction, run }
}

test("delivers only after admission, keeps attachments intact, and switches via the tool", async () => {
  const s = await setup()
  assert.equal(s.state.switched.length, 0)
  assert.equal(s.prompt.prompt.text, "Test task")
  assert.deepEqual(s.prompt.prompt.files[0].mention, { start: 0, end: 4 })
  const before = s.event()
  await s.instruction(before)
  assert.equal(before.messages.length, 0)
  s.admit()
  const after = s.event()
  await s.instruction(after)
  assert.equal(after.messages.length, 1)
  assert.equal(after.system.length, 0)
  assert.match(after.messages[0].content[0].text, /"model":"mistral\/small#high"/)
  assert.match(after.messages[0].content[0].text, /"routing":"initial"/)
  assert.equal(s.state.switched.length, 0)
  await s.run()
  assert.equal(s.state.switched.length, 1)
  assert.equal(s.records.get(markerKey("ses_test", "initial")).status, "switched")
  const next = s.event()
  await s.instruction(next)
  assert.equal(next.messages.length, 0)
  await assert.rejects(s.run(), /no longer valid/)
})

test("supports Build, Plan, Alloy, custom agents and the routed-only reviewer", async () => {
  for (const agent of ["build", "plan", "alloy", "custom", "reviewer"]) {
    const s = await setup(agent, undefined)
    s.state.session.model = undefined
    s.admit()
    const e = s.event()
    await s.instruction(e)
    assert.equal(e.messages.length, 1, agent)
    await s.run()
    assert.equal(s.state.switched.length, 1, agent)
    if (agent === "reviewer") await assert.rejects(s.run({ routing: undefined }), /authorized Auto handoff/)
  }
})

test("supports explicit mid-session Auto epochs and wrapped history", async () => {
  const history = [{ type: "user", id: "msg_old" }, { type: "model-switched", id: "msg_auto", model: { providerID: "mistral", id: "auto" } }]
  const s = await setup("reviewer", AUTO, history)
  s.admit()
  s.ctx.session.context = async () => ({ data: s.state.history })
  s.state.history.at(-1).payload = { metadata: s.prompt.metadata }
  delete s.state.history.at(-1).metadata
  const e = s.event()
  await s.instruction(e)
  assert.match(e.messages[0].content[0].text, /"routing":"msg_auto"/)
  await s.run()
  assert.equal(s.state.switched.length, 1)
})

test("reselecting Auto after a completed handoff delivers a new current-epoch reminder", async () => {
  const s = await setup()
  s.admit()
  await s.run()
  s.state.session.model = AUTO
  s.state.history.push({ type: "model-switched", id: "msg_auto_again", model: AUTO })
  const prompt = { sessionID: "ses_test", messageID: "msg_user_again", prompt: { text: "Continue the task" } }
  await createFirstPromptRouter(s.ctx)(prompt)
  s.state.history.push({ type: "user", id: prompt.messageID, text: prompt.prompt.text, metadata: prompt.metadata })
  const e = s.event()
  e.messages.push({ role: "user", content: [{ type: "text", text: prompt.prompt.text }] })
  await s.instruction(e)
  assert.equal(e.messages.length, 2)
  const reminder = e.messages.at(-1)
  assert.equal(reminder.role, "user")
  assert.match(reminder.content[0].text, /CURRENT pending Auto selection: msg_auto_again/)
  assert.match(reminder.content[0].text, /Earlier switch_model calls.*do NOT satisfy/)
  assert.match(reminder.content[0].text, /"routing":"msg_auto_again"/)
  assert.equal(prompt.prompt.text, "Continue the task")
  await s.run({ routing: "msg_auto_again" })
  assert.equal(s.state.switched.length, 2)
})

const changes = {
  "not admitted": () => {},
  "manual concrete selection": (s) => { s.state.session.model = { providerID: "mistral", id: "large" } },
  "changed Auto variant": (s) => { s.state.session.model = { ...AUTO, variant: "high" } },
  "changed agent": (s) => { s.state.session.agent = "plan" },
  "new Auto epoch": (s) => { s.state.history.push({ type: "model-switched", id: "msg_new_auto", model: AUTO }) },
  "later user prompt": (s) => { s.state.history.push({ type: "user", id: "msg_later", text: "Continue" }) },
  "forged prompt metadata": (s) => { s.state.history.at(-1).metadata.sessionModelRouting.model = "mistral/large" },
  "no plugin marker": (s) => { s.records.clear() },
  "different admitted message": (s) => { s.state.history.at(-1).id = "msg_other" },
  "changed shortlist": (s) => { s.ctx.options.routing.candidates = [] },
  "disabled routing": (s) => { s.ctx.options.routing.enabled = false },
}
for (const [name, change] of Object.entries(changes)) {
  test(`does not instruct or execute a stale handoff: ${name}`, async () => {
    const s = await setup("reviewer")
    if (name !== "not admitted") s.admit()
    change(s)
    const e = s.event()
    await s.instruction(e)
    assert.equal(e.messages.length, 0)
    await assert.rejects(s.run(), /no longer valid/)
    assert.equal(s.state.switched.length, 0)
  })
}

test("revalidates policy and availability, and does not repeat a failed handoff", async () => {
  for (const change of [
    (s) => { s.ctx.options.models = ["other/*"] },
    (s) => { s.ctx.model.list = async () => [] },
    (s) => { s.ctx.session.switchModel = async () => { throw new Error("Switch failed") } },
  ]) {
    const s = await setup("reviewer")
    s.admit()
    change(s)
    await assert.rejects(s.run())
    assert.equal(s.records.get(markerKey("ses_test", "initial")).status, "failed")
    const e = s.event()
    await s.instruction(e)
    assert.equal(e.messages.length, 0)
    await assert.rejects(s.run(), /no longer valid/)
  }
})

test("rechecks selection after catalog validation", async () => {
  const s = await setup("reviewer")
  s.admit()
  const list = s.ctx.model.list
  s.ctx.model.list = async () => {
    s.state.session.model = { providerID: "mistral", id: "large" }
    return list()
  }
  await assert.rejects(s.run(), /changed before switching/)
  assert.equal(s.state.switched.length, 0)
})

test("rejects another target even for the reviewer and prevents concurrent/replayed calls", async () => {
  const s = await setup("reviewer")
  s.admit()
  await assert.rejects(s.run({ model: "mistral/large" }), /no longer valid/)
  // A rejected attempt consumes the authorized handoff rather than looping.
  assert.equal(s.state.switched.length, 0)
  const t = await setup("reviewer")
  t.admit()
  const results = await Promise.allSettled([t.run(), t.run()])
  assert.equal(results.filter((item) => item.status === "fulfilled").length, 1)
  assert.equal(t.state.switched.length, 1)
})

test("read failures and absent switch permission do not inject instructions", async () => {
  const s = await setup()
  s.admit()
  const noTool = s.event()
  delete noTool.tools.switch_model
  await s.instruction(noTool)
  assert.equal(noTool.messages.length, 0)
  s.ctx.session.context = async () => { throw new Error("PRIVATE") }
  const e = s.event()
  await s.instruction(e)
  assert.equal(e.messages.length, 0)
})

test("instruction lookup is bounded and late reads cannot mutate the model request", async () => {
  const s = await setup()
  s.admit()
  let release
  s.ctx.session.context = () => new Promise((resolve) => { release = resolve })
  const e = s.event()
  await s.instruction(e)
  assert.equal(e.messages.length, 0)
  release(s.state.history)
  await new Promise((resolve) => setImmediate(resolve))
  assert.equal(e.messages.length, 0)
})

test("does not replay old direct-switch markers or incomplete attempts", async () => {
  for (const status of ["selected", "fallback", "uncertain", "pending", "dispatching", "failed", "switched"]) {
    const s = await setup("reviewer")
    s.admit()
    const key = markerKey("ses_test", "initial")
    s.records.set(key, { ...s.records.get(key), status })
    const e = s.event()
    await s.instruction(e)
    assert.equal(e.messages.length, 0, status)
    await assert.rejects(s.run(), /no longer valid/)
  }
})

test("storage failures cannot authorize an untracked switch or undo a confirmed one", async () => {
  const s = await setup("reviewer")
  s.admit()
  s.ctx.storage.set = async () => { throw new Error("Disk failed") }
  await assert.rejects(s.run(), /Disk failed/)
  assert.equal(s.state.switched.length, 0)

  const t = await setup("reviewer")
  t.admit()
  const set = t.ctx.storage.set
  t.ctx.storage.set = (key, value) => {
    if (value.status === "switched") throw new Error("Disk failed")
    return set(key, value)
  }
  await t.run()
  assert.equal(t.state.switched.length, 1)
  const e = t.event()
  await t.instruction(e)
  assert.equal(e.messages.length, 0)
})
