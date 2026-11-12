import assert from "node:assert/strict"
import test from "node:test"
import { createFirstPromptRouter } from "../first-prompt-router.js"
import plugin from "../index.js"

const AUTO = { providerID: "mistral", id: "auto" }
const target = "mistral/small#high"
const routing = {
  selector: "mistral/large#medium",
  candidates: [{ model: target, description: "Economical model with ample reasoning" }],
  timeoutMs: 1000,
}

const defaultModels = [
  { providerID: "mistral", id: "auto", modelID: "zai-glm-5-3-flash", variants: [] },
  { providerID: "mistral", id: "large", variants: [{ id: "medium" }] },
  { providerID: "mistral", id: "small", variants: ["high"] },
]

function deferred() {
  let resolve
  const promise = new Promise((done) => { resolve = done })
  return { promise, resolve }
}

function setup(overrides = {}) {
  const records = overrides.records ?? new Map()
  const calls = { generated: [], switched: [], listed: 0, agentGets: 0, defaults: 0, contexts: 0 }
  const state = {
    session: { agent: "build", model: AUTO, ...overrides.session },
    history: overrides.history ?? [],
  }
  const modelList = typeof overrides.models === "function"
    ? overrides.models
    : () => overrides.models ?? defaultModels
  const agentGet = typeof overrides.agent === "function"
    ? overrides.agent
    : () => overrides.agent ?? { model: undefined }
  const defaultModel = typeof overrides.defaultModel === "function"
    ? overrides.defaultModel
    : () => overrides.defaultModel ?? AUTO
  const ctx = {
    options: { agents: ["alloy"], models: ["mistral/*"], routing, ...overrides.options },
    storage: {
      get: async (key) => records.get(key),
      set: async (key, value) => { records.set(key, value) },
      ...overrides.storage,
    },
    agent: {
      get: async (input) => {
        calls.agentGets++
        return agentGet(input, state)
      },
    },
    model: {
      list: async () => {
        calls.listed++
        return modelList(calls.listed, state)
      },
      default: async () => {
        calls.defaults++
        return defaultModel(state)
      },
    },
    session: {
      get: async () => overrides.get ? overrides.get(state) : state.session,
      context: async () => {
        calls.contexts++
        return overrides.context ? overrides.context(state) : state.history
      },
      switchModel: async (input) => {
        calls.switched.push(input)
        if (overrides.applySwitch !== false) state.session = { ...state.session, model: input.model }
      },
    },
    experimental: { generate: { text: async (input) => {
      calls.generated.push({ input })
      if (overrides.generate) return overrides.generate(input, state)
      return { text: JSON.stringify({ model: target, reason: "Sufficient reasoning for a simple task." }) }
    } } },
  }
  const event = (prompt = {}) => ({
    sessionID: "ses_test",
    messageID: "msg_test",
    prompt: { text: "Fix a typo", ...prompt },
    metadata: { source: "test" },
  })
  return { ctx, records, calls, state, event, run: createFirstPromptRouter(ctx) }
}

const autoEvent = (id, created = 2) => ({
  id,
  type: "model-switched",
  model: AUTO,
  previous: { providerID: "mistral", id: "small", variant: "high" },
  time: { created },
})

test("routes a fresh root Auto selection before admission", async () => {
  const { run, event, calls, records } = setup()
  const input = event({ text: "x".repeat(30000) })
  await run(input)
  assert.deepEqual(calls.switched, [{ sessionID: "ses_test", model: { providerID: "mistral", id: "small", variant: "high" } }])
  assert.equal(input.metadata.sessionModelRouting.status, "selected")
  assert.equal(input.metadata.sessionModelRouting.model, target)
  assert.equal(input.metadata.source, "test")
  assert.equal(records.get("routing/v2/ses_test/initial").status, "selected")
  assert.equal(calls.generated.length, 1)
  assert.match(calls.generated[0].input.prompt, /"promptLength":30000/)
  assert.match(calls.generated[0].input.prompt, /"hasAttachments":false/)
  assert.doesNotMatch(calls.generated[0].input.prompt, /mistral\/auto/)
  assert.equal(calls.agentGets, 0)
  assert.equal(calls.defaults, 0)
})

test("routes Auto regardless of the selected primary agent", async () => {
  for (const agent of ["build", "plan", "alloy", "custom"]) {
    const { run, event, calls } = setup({ session: { agent } })
    await run(event())
    assert.equal(calls.switched.length, 1)
  }
})

test("leaves concretely selected roots untouched", async () => {
  for (const model of [
    { providerID: "mistral", id: "small", variant: "high" },
    { providerID: "openai", id: "other" },
    { providerID: "mistral", id: "automatic" },
  ]) {
    const { run, event, calls } = setup({ session: { model } })
    await run(event())
    assert.equal(calls.listed, 0)
    assert.equal(calls.generated.length, 0)
    assert.equal(calls.switched.length, 0)
    assert.equal(calls.agentGets, 0)
    assert.equal(calls.defaults, 0)
  }
})

test("routes Auto children but leaves pinned children untouched", async () => {
  const auto = setup({ session: { parentID: "ses_parent", model: AUTO } })
  const input = auto.event()
  await auto.run(input)
  assert.equal(auto.calls.switched.length, 1)
  assert.equal(auto.calls.switched[0].sessionID, "ses_test")
  assert.equal(input.metadata.sessionModelRouting.status, "selected")

  const pinned = setup({ session: { parentID: "ses_parent", model: { providerID: "mistral", id: "small", variant: "high" } } })
  await pinned.run(pinned.event())
  assert.equal(pinned.calls.generated.length, 0)
  assert.equal(pinned.calls.switched.length, 0)
})

test("routes existing substantive history with an explicit Auto selection", async () => {
  const history = [{ type: "user" }, { type: "assistant" }, autoEvent("msg_auto_2")]
  const { run, event, calls, records } = setup({ history })
  await run(event())
  assert.equal(calls.generated.length, 1)
  assert.equal(calls.switched.length, 1)
  assert.equal(records.get("routing/v2/ses_test/msg_auto_2").status, "selected")
})

test("routes a manual concrete-to-Auto selection whose event omits the default variant", async () => {
  const liveAuto = { providerID: "mistral", id: "auto", variant: "default" }
  const liveEvent = {
    id: "msg_live_auto",
    type: "model-switched",
    model: { id: "auto", providerID: "mistral" },
    previous: { providerID: "mistral", id: "small", variant: "high" },
    time: { created: 1794431602863 },
  }

  const selectionOnly = setup({ session: { model: liveAuto }, history: [liveEvent] })
  const input = selectionOnly.event()
  await selectionOnly.run(input)
  assert.equal(selectionOnly.calls.generated.length, 1)
  assert.equal(selectionOnly.calls.switched.length, 1)
  assert.equal(input.metadata.sessionModelRouting.status, "selected")
  assert.equal(selectionOnly.records.get("routing/v2/ses_test/msg_live_auto").status, "selected")

  const substantive = setup({
    session: { model: liveAuto },
    history: [{ type: "user" }, { type: "assistant" }, liveEvent],
  })
  await substantive.run(substantive.event())
  assert.equal(substantive.calls.generated.length, 1)
  assert.equal(substantive.calls.switched.length, 1)

  // A real variant difference remains inconsistent and is not routed.
  const mismatched = setup({
    session: { model: { ...liveAuto, variant: "high" } },
    history: [liveEvent],
  })
  await mismatched.run(mismatched.event())
  assert.equal(mismatched.calls.generated.length, 0)
  assert.equal(mismatched.calls.switched.length, 0)
})

test("selection-only history uses the explicit Auto event or the stable initial epoch", async () => {
  const explicit = setup({ history: [
    { type: "agent-switched", agent: "build" },
    { id: "msg_auto_initial", type: "model-switched", model: AUTO, time: { created: 1 } },
    { type: "location-switched" },
  ] })
  await explicit.run(explicit.event())
  assert.equal(explicit.calls.generated.length, 1)
  assert.equal(explicit.calls.switched.length, 1)
  assert.equal(explicit.records.get("routing/v2/ses_test/msg_auto_initial").status, "selected")

  const defaulted = setup({ history: [
    { type: "agent-switched", agent: "build" },
    { type: "location-switched" },
  ] })
  await defaulted.run(defaulted.event())
  assert.equal(defaulted.calls.generated.length, 1)
  assert.equal(defaulted.calls.switched.length, 1)
  assert.equal(defaulted.records.get("routing/v2/ses_test/initial").status, "selected")
})

test("an explicit Auto event keeps its epoch after the first prompt becomes substantive", async () => {
  const history = [{ type: "agent-switched", agent: "build" }, autoEvent("msg_auto_fresh", 1)]
  const { run, event, calls, records, state } = setup({
    history,
    applySwitch: false,
    generate: async () => ({ text: "not JSON" }),
  })
  await run(event())
  assert.equal(calls.generated.length, 1)
  assert.equal(records.get("routing/v2/ses_test/msg_auto_fresh").status, "fallback")
  state.history = [...history, { type: "user" }, { type: "assistant" }]
  await run(event())
  assert.equal(calls.generated.length, 1)
  assert.equal(calls.switched.length, 0)
})

test("skips old substantive history without a surviving Auto selection event", async () => {
  for (const history of [
    [{ type: "user" }, { type: "assistant" }],
    [{ type: "user" }, { type: "assistant" }, { id: "msg_concrete", type: "model-switched", model: { providerID: "mistral", id: "small", variant: "high" } }],
    [{ type: "user" }, { type: "assistant" }, autoEvent("msg_auto_1"), { id: "msg_concrete", type: "model-switched", model: { providerID: "mistral", id: "small", variant: "high" } }],
    [{ type: "user" }, { type: "assistant" }, { id: "msg_unknown", type: "model-switched" }],
  ]) {
    const { run, event, calls } = setup({ history })
    await run(event())
    assert.equal(calls.generated.length, 0)
    assert.equal(calls.switched.length, 0)
  }
})

test("skips an unfinished durable attempt for the same epoch", async () => {
  const { run, event, calls } = setup({ records: new Map([["routing/v2/ses_test/initial", { status: "pending" }]]) })
  await run(event())
  assert.equal(calls.listed, 0)
  assert.equal(calls.generated.length, 0)
  assert.equal(calls.switched.length, 0)
})

test("missing or disabled routing does nothing", async () => {
  for (const value of [undefined, { enabled: false }]) {
    const { run, event, calls } = setup({ options: { routing: value } })
    await run(event())
    assert.equal(calls.listed, 0)
    assert.equal(calls.generated.length, 0)
  }
})

test("resolves an undefined initial model like the runner", async () => {
  const base = { session: { agent: "build", model: undefined } }

  const byDefault = setup({ ...base, agent: () => ({ model: undefined }), defaultModel: () => AUTO })
  await byDefault.run(byDefault.event())
  assert.equal(byDefault.calls.generated.length, 1)
  assert.equal(byDefault.calls.switched.length, 1)
  assert.equal(byDefault.calls.defaults, 2)
  assert.equal(byDefault.records.get("routing/v2/ses_test/initial").status, "selected")

  const agentPin = setup({
    ...base,
    agent: () => ({ model: { providerID: "mistral", id: "small", variant: "high" } }),
    defaultModel: () => AUTO,
  })
  await agentPin.run(agentPin.event())
  assert.equal(agentPin.calls.generated.length, 0)
  assert.equal(agentPin.calls.switched.length, 0)
  assert.equal(agentPin.calls.defaults, 0)
  assert.equal(agentPin.calls.listed, 1)

  const unavailablePin = setup({
    ...base,
    agent: () => ({ model: { providerID: "mistral", id: "missing", variant: "high" } }),
    defaultModel: () => AUTO,
  })
  await unavailablePin.run(unavailablePin.event())
  assert.equal(unavailablePin.calls.generated.length, 1)
  assert.equal(unavailablePin.calls.switched.length, 1)
  assert.equal(unavailablePin.calls.defaults, 2)

  const unreadableCatalog = setup({
    ...base,
    agent: () => ({ model: { providerID: "mistral", id: "small", variant: "high" } }),
    models: () => { throw new Error("catalog unavailable") },
    defaultModel: () => AUTO,
  })
  await unreadableCatalog.run(unreadableCatalog.event())
  assert.equal(unreadableCatalog.calls.generated.length, 0)
  assert.equal(unreadableCatalog.calls.switched.length, 0)
  assert.equal(unreadableCatalog.calls.defaults, 0)

  const agentAuto = setup({
    ...base,
    agent: () => ({ model: AUTO }),
    defaultModel: () => ({ providerID: "mistral", id: "small", variant: "high" }),
  })
  await agentAuto.run(agentAuto.event())
  assert.equal(agentAuto.calls.generated.length, 1)
  assert.equal(agentAuto.calls.switched.length, 1)
  assert.equal(agentAuto.calls.defaults, 0)

  const unreadableAgent = setup({
    ...base,
    agent: () => { throw new Error("agent unavailable") },
    defaultModel: () => AUTO,
  })
  await unreadableAgent.run(unreadableAgent.event())
  assert.equal(unreadableAgent.calls.generated.length, 1)
  assert.equal(unreadableAgent.calls.switched.length, 1)

  const noAgent = setup({ session: { agent: undefined, model: undefined }, defaultModel: () => AUTO })
  await noAgent.run(noAgent.event())
  assert.equal(noAgent.calls.generated.length, 1)
  assert.equal(noAgent.calls.agentGets, 0)

  const noDefault = setup({ ...base, agent: () => ({ model: undefined }), defaultModel: () => null })
  await noDefault.run(noDefault.event())
  assert.equal(noDefault.calls.generated.length, 0)
  assert.equal(noDefault.calls.switched.length, 0)

  const wrapped = setup({
    ...base,
    agent: () => ({ data: { model: AUTO } }),
    defaultModel: () => ({ data: AUTO }),
  })
  await wrapped.run(wrapped.event())
  assert.equal(wrapped.calls.generated.length, 1)
  assert.equal(wrapped.calls.switched.length, 1)
})

test("dedupes the same Auto epoch across follow-ups, reloads and fallbacks", async () => {
  const history = [{ type: "user" }, { type: "assistant" }, autoEvent("msg_auto_2")]
  const selected = setup({ history, applySwitch: false })
  await selected.run(selected.event())
  assert.equal(selected.calls.generated.length, 1)
  selected.state.history = [...history, { type: "user" }, { type: "assistant" }, { type: "agent-switched", agent: "plan" }]
  selected.state.session = { ...selected.state.session, agent: "plan", model: AUTO }
  await selected.run(selected.event())
  await createFirstPromptRouter(selected.ctx)(selected.event())
  assert.equal(selected.calls.generated.length, 1)
  assert.equal(selected.calls.switched.length, 1)
  assert.equal(selected.records.get("routing/v2/ses_test/msg_auto_2").status, "selected")

  const fallback = setup({ history, applySwitch: false, generate: async () => ({ text: "not JSON" }) })
  await fallback.run(fallback.event())
  await fallback.run(fallback.event())
  await createFirstPromptRouter(fallback.ctx)(fallback.event())
  assert.equal(fallback.calls.generated.length, 1)
  assert.equal(fallback.calls.switched.length, 0)
  assert.equal(fallback.records.get("routing/v2/ses_test/msg_auto_2").status, "fallback")

  const fresh = setup({ applySwitch: false })
  await fresh.run(fresh.event())
  await fresh.run(fresh.event())
  await createFirstPromptRouter(fresh.ctx)(fresh.event())
  assert.equal(fresh.calls.generated.length, 1)
  assert.equal(fresh.records.get("routing/v2/ses_test/initial").status, "selected")
})

test("a new concrete-to-Auto selection event creates a new epoch", async () => {
  const history = [{ type: "user" }, { type: "assistant" }, autoEvent("msg_auto_1")]
  const { run, event, calls, records, state } = setup({ history, applySwitch: false })
  await run(event())
  assert.equal(calls.generated.length, 1)
  state.history = [
    ...history,
    { id: "msg_concrete", type: "model-switched", model: { providerID: "mistral", id: "small", variant: "high" } },
    autoEvent("msg_auto_2", 3),
  ]
  await run(event())
  assert.equal(calls.generated.length, 2)
  assert.equal(calls.switched.length, 2)
  assert.equal(records.get("routing/v2/ses_test/msg_auto_2").status, "selected")
  await run(event())
  assert.equal(calls.generated.length, 2)
})

test("skips Auto inherited by a fork unless Auto is explicitly selected after the fork", async () => {
  const inherited = autoEvent("msg_auto_inherited", 500)
  const fork = (history, time = { created: 1000 }) => setup({
    session: {
      model: AUTO,
      fork: { sessionID: "ses_parent", boundary: { type: "through", messageID: "msg_parent" } },
      time,
    },
    history,
  })

  const unchanged = fork([{ type: "user" }, { type: "assistant" }, inherited])
  await unchanged.run(unchanged.event())
  assert.equal(unchanged.calls.generated.length, 0)
  assert.equal(unchanged.calls.switched.length, 0)

  const agentOnly = fork([{ type: "user" }, { type: "assistant" }, inherited, { type: "agent-switched", agent: "plan" }])
  await agentOnly.run(agentOnly.event())
  assert.equal(agentOnly.calls.generated.length, 0)
  assert.equal(agentOnly.calls.switched.length, 0)

  const freshInherited = fork([{ type: "agent-switched", agent: "build" }, inherited])
  await freshInherited.run(freshInherited.event())
  assert.equal(freshInherited.calls.generated.length, 0)
  assert.equal(freshInherited.calls.switched.length, 0)

  const reselected = fork([{ type: "user" }, { type: "assistant" }, inherited, autoEvent("msg_auto_fork", 1001)])
  await reselected.run(reselected.event())
  assert.equal(reselected.calls.generated.length, 1)
  assert.equal(reselected.calls.switched.length, 1)
  assert.equal(reselected.records.get("routing/v2/ses_test/msg_auto_fork").status, "selected")

  const concreteThenAuto = fork([
    { type: "user" },
    { type: "assistant" },
    inherited,
    { id: "msg_concrete_fork", type: "model-switched", model: { providerID: "mistral", id: "small", variant: "high" }, time: { created: 1000 } },
    autoEvent("msg_auto_fork_2", 1001),
  ])
  await concreteThenAuto.run(concreteThenAuto.event())
  assert.equal(concreteThenAuto.calls.generated.length, 1)
  assert.equal(concreteThenAuto.calls.switched.length, 1)

  const missingForkTime = fork([{ type: "user" }, { type: "assistant" }, autoEvent("msg_auto_fork_3", 1000)], {})
  await missingForkTime.run(missingForkTime.event())
  assert.equal(missingForkTime.calls.generated.length, 0)
})

test("serializes concurrent admissions and routes one generation per epoch", async () => {
  const gate = deferred()
  const started = deferred()
  const { run, event, calls } = setup({
    applySwitch: false,
    generate: async () => {
      started.resolve()
      await gate.promise
      return { text: JSON.stringify({ model: target, reason: "Enough" }) }
    },
  })
  const firstInput = event()
  const first = run(firstInput)
  await started.promise
  const secondInput = event()
  const second = run(secondInput)
  gate.resolve()
  await Promise.all([first, second])
  assert.equal(calls.generated.length, 1)
  assert.equal(calls.switched.length, 1)
  assert.equal(firstInput.metadata.sessionModelRouting.status, "selected")
  assert.equal(secondInput.metadata.sessionModelRouting, undefined)
})

test("aborts a stale Auto epoch and lets a queued admission route the new one", async () => {
  const gate = deferred()
  const started = deferred()
  const { run, event, calls, records, state } = setup({
    applySwitch: false,
    generate: async () => {
      started.resolve()
      await gate.promise
      return { text: JSON.stringify({ model: target, reason: "Enough" }) }
    },
  })
  const firstInput = event()
  const first = run(firstInput)
  await started.promise
  const secondInput = event()
  const second = run(secondInput)
  state.session = { ...state.session, model: AUTO }
  state.history = [{ type: "user" }, { type: "assistant" }, autoEvent("msg_auto_2")]
  gate.resolve()
  await Promise.all([first, second])
  assert.equal(firstInput.metadata.sessionModelRouting.status, "fallback")
  assert.equal(secondInput.metadata.sessionModelRouting.status, "selected")
  assert.equal(calls.generated.length, 2)
  assert.equal(calls.switched.length, 1)
  assert.equal(records.get("routing/v2/ses_test/initial").status, "fallback")
  assert.equal(records.get("routing/v2/ses_test/msg_auto_2").status, "selected")
})

test("keeps queued admissions routing when live Auto events omit the default variant", async () => {
  const gate = deferred()
  const started = deferred()
  const liveAuto = { providerID: "mistral", id: "auto", variant: "default" }
  const liveEvent = (id, created) => ({
    id,
    type: "model-switched",
    model: { id: "auto", providerID: "mistral" },
    previous: { providerID: "mistral", id: "small", variant: "high" },
    time: { created },
  })
  const { run, event, calls, records, state } = setup({
    session: { model: liveAuto },
    history: [{ type: "user" }, { type: "assistant" }, liveEvent("msg_live_auto_1", 1794431602863)],
    applySwitch: false,
    generate: async () => {
      started.resolve()
      await gate.promise
      return { text: JSON.stringify({ model: target, reason: "Enough" }) }
    },
  })
  const firstInput = event()
  const first = run(firstInput)
  await started.promise
  const secondInput = event()
  const second = run(secondInput)
  state.history = [
    ...state.history,
    liveEvent("msg_live_auto_2", 1794431602864),
  ]
  gate.resolve()
  await Promise.all([first, second])
  assert.equal(firstInput.metadata.sessionModelRouting.status, "fallback")
  assert.equal(secondInput.metadata.sessionModelRouting.status, "selected")
  assert.equal(calls.generated.length, 2)
  assert.equal(calls.switched.length, 1)
  assert.equal(records.get("routing/v2/ses_test/msg_live_auto_1").status, "fallback")
  assert.equal(records.get("routing/v2/ses_test/msg_live_auto_2").status, "selected")
})

test("treats busy as a watermark, not an active flag, in the final guard", async () => {
  const liveAuto = { providerID: "mistral", id: "auto", variant: "default" }
  const history = [
    { type: "agent-switched", agent: "build" },
    { type: "user" },
    { type: "assistant" },
    { type: "assistant" },
    {
      id: "msg_reviewer_auto",
      type: "model-switched",
      model: { id: "auto", providerID: "mistral" },
      previous: { providerID: "mistral", id: "small", variant: "high" },
      time: { created: 1794431606000 },
    },
  ]
  const completed = {
    parentID: "ses_parent",
    model: liveAuto,
    time: { created: 1794431600000, busy: 1794431601000, idle: 1794431605000 },
  }

  const finished = setup({ session: completed, history })
  const finishedInput = finished.event()
  await finished.run(finishedInput)
  assert.equal(finished.calls.generated.length, 1)
  assert.equal(finished.calls.switched.length, 1)
  assert.equal(finishedInput.metadata.sessionModelRouting.status, "selected")

  for (const time of [
    { created: 1794431600000, busy: 1794431609000, idle: 1794431605000 },
    { created: 1794431600000, busy: 1794431605000, idle: 1794431605000 },
    { created: 1794431600000, busy: 1794431605000 },
  ]) {
    const active = setup({ session: { ...completed, time }, history })
    const input = active.event()
    await active.run(input)
    assert.equal(active.calls.generated.length, 1)
    assert.equal(active.calls.switched.length, 0)
    assert.equal(input.metadata.sessionModelRouting.status, "fallback")
  }

  const noBusy = setup({
    session: { ...completed, time: { created: 1794431600000, idle: 1794431605000 } },
    history,
  })
  await noBusy.run(noBusy.event())
  assert.equal(noBusy.calls.switched.length, 1)
})

test("preserves a manual model, variant, agent or busy change made while the selector runs", async () => {
  for (const change of [
    (state) => { state.session = { ...state.session, model: { providerID: "openai", id: "manual" } } },
    (state) => { state.session = { ...state.session, model: { providerID: "mistral", id: "auto", variant: "high" } } },
    (state) => { state.session = { ...state.session, agent: "plan" } },
    (state) => { state.session = { ...state.session, time: { ...state.session.time, busy: 1794431609000 } } },
  ]) {
    const { run, event, calls, state } = setup({
      applySwitch: false,
      generate: async () => {
        change(state)
        return { text: JSON.stringify({ model: target, reason: "Enough" }) }
      },
    })
    const input = event()
    await run(input)
    assert.equal(calls.switched.length, 0)
    assert.equal(input.metadata.sessionModelRouting.status, "fallback")
  }
})

test("never offers or accepts the Auto alias as a routing target", async () => {
  const filtered = setup({ options: { routing: {
    ...routing,
    candidates: [
      { model: "mistral/auto", description: "Alias" },
      { model: "mistral/auto#high", description: "Alias variant" },
      ...routing.candidates,
    ],
  } } })
  await filtered.run(filtered.event())
  assert.equal(filtered.calls.generated.length, 1)
  assert.doesNotMatch(filtered.calls.generated[0].input.prompt, /mistral\/auto/)
  assert.equal(filtered.calls.switched.length, 1)

  const chosen = setup({ generate: async () => ({ text: JSON.stringify({ model: "mistral/auto", reason: "No" }) }) })
  const chosenInput = chosen.event()
  await chosen.run(chosenInput)
  assert.equal(chosen.calls.generated.length, 1)
  assert.equal(chosen.calls.switched.length, 0)
  assert.equal(chosenInput.metadata.sessionModelRouting.status, "fallback")

  const selector = setup({ options: { routing: { ...routing, selector: "mistral/auto" } } })
  await selector.run(selector.event())
  assert.equal(selector.calls.generated.length, 0)
  assert.equal(selector.records.get("routing/v2/ses_test/initial").status, "fallback")

  const allAuto = setup({ options: { routing: {
    ...routing,
    candidates: [{ model: "mistral/auto", description: "Alias" }],
  } } })
  await allAuto.run(allAuto.event())
  assert.equal(allAuto.calls.generated.length, 0)
  assert.equal(allAuto.records.get("routing/v2/ses_test/initial").status, "fallback")
})

test("filters unavailable, disabled, forbidden and unknown-variant candidates internally", async () => {
  const { run, event, calls } = setup({
    options: { routing: {
      ...routing,
      candidates: [
        ...routing.candidates,
        { model: "mistral/missing", description: "Unavailable" },
        { model: "mistral/small#unknown", description: "Unknown effort" },
        { model: "openai/other", description: "Forbidden provider" },
        { model: "mistral/disabled", description: "Disabled model" },
        { model: "mistral/auto", description: "Alias" },
      ],
    } },
    models: [
      { providerID: "mistral", id: "auto", modelID: "zai-glm-5-3-flash" },
      { providerID: "mistral", id: "large", variants: ["medium"] },
      { providerID: "mistral", id: "small", variants: ["high"] },
      { providerID: "openai", id: "other" },
      { providerID: "mistral", id: "disabled", enabled: false },
    ],
  })
  await run(event())
  assert.equal(calls.switched.length, 1)
  assert.doesNotMatch(calls.generated[0].input.prompt, /missing|unknown|openai\/other|mistral\/disabled|mistral\/auto/)
})

test("fails open and does not retry malformed, forbidden or failed selector output", async () => {
  for (const generate of [
    async () => { throw new Error("PRIVATE provider diagnostic") },
    async () => ({ text: "Not JSON" }),
    async () => ({ text: JSON.stringify({ model: "openai/other", reason: "No" }) }),
    async () => ({ text: JSON.stringify({ model: target, reason: "" }) }),
  ]) {
    const { run, event, calls, records } = setup({ generate })
    const input = event()
    await run(input)
    await run(event())
    assert.equal(calls.generated.length, 1)
    assert.equal(calls.switched.length, 0)
    assert.equal(records.get("routing/v2/ses_test/initial").status, "fallback")
    assert.doesNotMatch(JSON.stringify(input.metadata), /PRIVATE/)
  }
})

test("timeout releases admission without cancellation and ignores a late selector result", async () => {
  let finish
  const { run, event, calls, records } = setup({
    options: { routing: { ...routing, timeoutMs: 25 } },
    applySwitch: false,
    generate: async () => new Promise((resolve) => { finish = resolve }),
  })
  const input = event()
  await completesWithin(() => run(input))
  assert.equal(input.metadata.sessionModelRouting.status, "fallback")
  assert.equal(records.get("routing/v2/ses_test/initial").status, "pending")
  assert.equal(calls.generated.length, 1)
  finish({ text: JSON.stringify({ model: target, reason: "Too late" }) })
  await new Promise((resolve) => setImmediate(resolve))
  assert.equal(calls.switched.length, 0)
})

async function completesWithin(run, milliseconds = 500) {
  let timer
  try {
    return await Promise.race([
      run(),
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error("Admission remained blocked")), milliseconds)
      }),
    ])
  } finally {
    clearTimeout(timer)
  }
}

const stalledOperations = [
  ["initial session read", (ctx, stall) => { ctx.session.get = () => stall }],
  ["stored marker read", (ctx, stall) => { ctx.storage.get = () => stall }],
  ["context read", (ctx, stall) => { ctx.session.context = () => stall }],
  ["pending marker write", (ctx, stall) => {
    const set = ctx.storage.set
    ctx.storage.set = (key, value) => value.status === "pending" ? stall : set(key, value)
  }],
  ["initial model validation", (ctx, stall) => { ctx.model.list = () => stall }],
  ["target revalidation", (ctx, stall) => {
    const list = ctx.model.list
    let calls = 0
    ctx.model.list = () => ++calls === 2 ? stall : list()
  }],
  ["final session read", (ctx, stall) => {
    const get = ctx.session.get
    let calls = 0
    ctx.session.get = () => ++calls === 2 ? stall : get()
  }],
  ["final context read", (ctx, stall) => {
    const context = ctx.session.context
    let calls = 0
    ctx.session.context = () => ++calls === 2 ? stall : context()
  }],
]

for (const [name, inject] of stalledOperations) {
  test(`deadline releases concurrent admissions after a stalled ${name}; late resolution cannot switch`, async () => {
    let release
    const stall = new Promise((resolve) => { release = resolve })
    const { ctx, run, event, calls } = setup({ options: { routing: { ...routing, timeoutMs: 25 } } })
    inject(ctx, stall)
    const input = event()
    await completesWithin(() => Promise.all([run(input), run(event())]))
    assert.equal(input.metadata.sessionModelRouting.status, "fallback")
    assert.equal(calls.switched.length, 0)
    release({ agent: "build", model: AUTO })
    await new Promise((resolve) => setImmediate(resolve))
    assert.equal(calls.switched.length, 0)
  })
}

test("deadline covers agent and default model resolution", async () => {
  for (const kind of ["agent", "default"]) {
    const stall = new Promise(() => {})
    const { run, event, calls } = setup({
      options: { routing: { ...routing, timeoutMs: 25 } },
      session: { agent: "build", model: undefined },
      agent: kind === "agent" ? () => stall : () => ({ model: undefined }),
      defaultModel: kind === "default" ? () => stall : () => AUTO,
    })
    const input = event()
    await completesWithin(() => run(input))
    assert.equal(input.metadata.sessionModelRouting.status, "fallback")
    assert.equal(calls.generated.length, 0)
    assert.equal(calls.switched.length, 0)
  }
})

test("deadline covers the final agent and default re-resolution", async () => {
  for (const kind of ["agent", "default"]) {
    let resolutions = 0
    const stall = new Promise(() => {})
    const { run, event, calls } = setup({
      options: { routing: { ...routing, timeoutMs: 25 } },
      session: { agent: "build", model: undefined },
      agent: kind === "agent"
        ? () => ++resolutions === 2 ? stall : { model: AUTO }
        : () => ({ model: undefined }),
      defaultModel: kind === "default"
        ? () => ++resolutions === 2 ? stall : AUTO
        : () => AUTO,
    })
    const input = event()
    await completesWithin(() => run(input))
    assert.equal(input.metadata.sessionModelRouting.status, "fallback")
    assert.equal(calls.generated.length, 1)
    assert.equal(calls.switched.length, 0)
  }
})

test("a waiting admission has a bounded wait instead of an indefinite queue", async () => {
  const gate = deferred()
  const started = deferred()
  const { ctx, run, event, calls } = setup({
    options: { routing: { ...routing, timeoutMs: 40 } },
    applySwitch: false,
    generate: async () => {
      started.resolve()
      await gate.promise
      return { text: JSON.stringify({ model: target, reason: "Enough" }) }
    },
  })
  const get = ctx.session.get
  let gets = 0
  ctx.session.get = () => ++gets === 2 ? new Promise(() => {}) : get()
  const first = run(event())
  await started.promise
  const secondInput = event()
  await completesWithin(() => run(secondInput))
  assert.ok(secondInput.metadata.sessionModelRouting === undefined ||
    secondInput.metadata.sessionModelRouting.status === "fallback")
  assert.equal(calls.generated.length, 1)
  gate.resolve()
  await first
  assert.equal(calls.switched.length, 0)
})

test("a switch already dispatched at timeout has an uncertain outcome, not a retained-model guarantee", async () => {
  let finish
  let applied = false
  const { ctx, run, event, records } = setup({ options: { routing: { ...routing, timeoutMs: 25 } } })
  ctx.session.switchModel = () => new Promise((resolve) => { finish = () => { applied = true; resolve() } })
  const input = event()
  await completesWithin(() => run(input))
  assert.equal(input.metadata.sessionModelRouting.status, "uncertain")
  assert.equal(records.get("routing/v2/ses_test/initial").status, "pending")
  finish()
  await new Promise((resolve) => setImmediate(resolve))
  assert.equal(applied, true)
  assert.equal(input.metadata.sessionModelRouting.status, "uncertain")
})

for (const status of ["selected", "fallback"]) {
  test(`a stalled ${status} result write cannot hold admission open`, async () => {
    const { ctx, run, event, records, calls } = setup({
      options: { routing: { ...routing, timeoutMs: 25 } },
      ...(status === "fallback" ? { generate: async () => ({ text: "not JSON" }) } : {}),
    })
    const set = ctx.storage.set
    let resultWrites = 0
    ctx.storage.set = (key, value) => {
      if (value.status === "pending") return set(key, value)
      resultWrites++
      return new Promise(() => {})
    }
    const input = event()
    await completesWithin(() => run(input))
    assert.equal(input.metadata.sessionModelRouting.status, status)
    assert.equal(calls.switched.length, status === "selected" ? 1 : 0)
    assert.equal(resultWrites, 1)
    assert.equal(records.get("routing/v2/ses_test/initial").status, "pending")
  })
}

test("in-flight guard is released after timeout, even before a durable marker could be saved", async () => {
  const { ctx, run, event, calls } = setup({ options: { routing: { ...routing, timeoutMs: 25 } } })
  const get = ctx.session.get
  ctx.session.get = () => new Promise(() => {})
  await completesWithin(() => run(event()))
  ctx.session.get = get
  await completesWithin(() => run(event()))
  assert.equal(calls.generated.length, 1)
  assert.equal(calls.switched.length, 1)
})

test("sequential API waits share one budget instead of each getting a fresh timeout", async () => {
  const { ctx, run, event, calls } = setup({ options: { routing: { ...routing, timeoutMs: 60 } } })
  const slow = (fn) => async (...args) => {
    await new Promise((resolve) => setTimeout(resolve, 25))
    return fn(...args)
  }
  ctx.session.get = slow(ctx.session.get)
  ctx.storage.get = slow(ctx.storage.get)
  ctx.session.context = slow(ctx.session.context)
  const input = event()
  await completesWithin(() => run(input))
  assert.equal(input.metadata.sessionModelRouting.status, "fallback")
  assert.equal(calls.generated.length, 0)
  assert.equal(calls.switched.length, 0)
  await new Promise((resolve) => setTimeout(resolve, 30))
  assert.equal(calls.generated.length, 0)
})

test("invalid configuration or policy retains current model without invoking selector", async () => {
  for (const options of [
    { routing: {} },
    { routing: { ...routing, timeoutMs: -1 } },
    { models: "mistral/*" },
    { models: ["openai/*"] },
    { routing: { ...routing, selector: "mistral/missing" } },
    { routing: { ...routing, selector: "mistral/auto" } },
    { routing: { ...routing, candidates: [{ model: "mistral/auto", description: "Alias" }] } },
  ]) {
    const { run, event, calls, records } = setup({ options })
    await run(event())
    assert.equal(calls.generated.length, 0)
    assert.equal(calls.switched.length, 0)
    assert.equal(records.get("routing/v2/ses_test/initial").status, "fallback")
  }
})

test("storage failure never blocks prompt admission or starts untracked generation", async () => {
  const { ctx, event, calls } = setup()
  ctx.storage.set = async () => { throw new Error("disk unavailable") }
  const input = event()
  await createFirstPromptRouter(ctx)(input)
  assert.equal(calls.generated.length, 0)
  assert.equal(input.metadata.sessionModelRouting.status, "fallback")
})

test("result storage failure does not misreport an already completed switch", async () => {
  const { ctx, event, calls, records } = setup()
  const set = ctx.storage.set
  ctx.storage.set = async (key, value) => {
    if (value.status !== "pending") throw new Error("disk unavailable")
    await set(key, value)
  }
  const input = event()
  await createFirstPromptRouter(ctx)(input)
  assert.equal(calls.switched.length, 1)
  assert.equal(input.metadata.sessionModelRouting.status, "selected")
  assert.equal(records.get("routing/v2/ses_test/initial").status, "pending")
})

test("applies the candidate variant and rejects a target removed after generation", async () => {
  const first = setup()
  await first.run(first.event())
  assert.deepEqual(first.calls.switched[0].model, { providerID: "mistral", id: "small", variant: "high" })

  const second = setup({ applySwitch: false })
  const list = second.ctx.model.list
  second.ctx.model.list = async () => second.calls.listed ? [] : list()
  const input = second.event()
  await second.run(input)
  assert.equal(second.calls.generated.length, 1)
  assert.equal(second.calls.switched.length, 0)
  assert.equal(input.metadata.sessionModelRouting.status, "fallback")
})

test("accepts fenced JSON and wrapped API responses", async () => {
  const { ctx, event, calls } = setup({
    generate: async () => ({ data: {
      text: '```json\n' + JSON.stringify({ model: target, reason: "Enough" }) + '\n```',
    } }),
    get: (state) => ({ data: state.session }),
    context: (state) => ({ data: state.history }),
    models: () => ({ data: defaultModels }),
  })
  await createFirstPromptRouter(ctx)(event())
  assert.equal(calls.switched.length, 1)
})

test("plugin registers the admission hook only when configured", async () => {
  for (const enabled of [true, false]) {
    const { ctx } = setup({ options: { routing: enabled ? routing : undefined } })
    const hooks = []
    const tools = []
    ctx.tool = { transform: async (edit) => edit({ add: (tool) => tools.push(tool) }) }
    ctx.session.hook = async (name, callback) => { hooks.push({ name, callback }) }
    await plugin.setup(ctx)
    assert.equal(tools.length, 2)
    assert.deepEqual(hooks.map((hook) => hook.name), enabled ? ["prompt"] : [])
  }
})
