import assert from "node:assert/strict"
import test from "node:test"
import { createFirstPromptRouter } from "../first-prompt-router.js"
import plugin from "../index.js"

const target = "mistral/small#high"
const routing = {
  selector: "mistral/large#medium",
  candidates: [{ model: target, description: "Economical model with ample reasoning" }],
  timeoutMs: 1000,
}

function setup(overrides = {}) {
  const records = overrides.records ?? new Map()
  const calls = { generated: [], switched: [], listed: 0 }
  const session = { agent: "build", model: { providerID: "openai", id: "initial" }, ...overrides.session }
  let gets = 0
  const ctx = {
    options: { agents: ["alloy"], models: ["mistral/*"], routing, ...overrides.options },
    storage: {
      get: async (key) => records.get(key),
      set: async (key, value) => { records.set(key, value) },
    },
    model: { list: async () => {
      calls.listed++
      return overrides.models ?? [
        { providerID: "mistral", id: "large", variants: [{ id: "medium" }] },
        { providerID: "mistral", id: "small", variants: ["high"] },
      ]
    } },
    session: {
      get: async () => {
        gets++
        return gets > 1 && overrides.latest ? overrides.latest : session
      },
      context: async () => overrides.history ?? [],
      switchModel: async (input) => { calls.switched.push(input) },
    },
    // Match SDK 2.2.6: its Promise adapter accepts only input, not RequestOptions.
    experimental: { generate: { text: async (input) => {
      calls.generated.push({ input })
      if (overrides.generate) return overrides.generate(input)
      return { text: JSON.stringify({ model: target, reason: "Sufficient reasoning for a simple task." }) }
    } } },
  }
  const event = () => ({ sessionID: "ses_test", messageID: "msg_test", prompt: { text: "Fix a typo" }, metadata: { source: "test" } })
  return { ctx, records, calls, event, run: createFirstPromptRouter(ctx) }
}

test("routes before admission regardless of selected primary agent or tool agent policy", async () => {
  for (const agent of ["build", "plan", "alloy", "custom"]) {
    const { run, event, calls } = setup({ session: { agent } })
    const input = event()
    await run(input)
    assert.deepEqual(calls.switched, [{ sessionID: "ses_test", model: { providerID: "mistral", id: "small", variant: "high" } }])
    assert.equal(input.metadata.sessionModelRouting.model, target)
    assert.equal(input.metadata.source, "test")
    assert.equal(input.prompt.text, "Fix a typo")
    assert.equal(calls.generated.length, 1)
    assert.doesNotMatch(calls.generated[0].input.prompt, /openai\/initial/)
  }
})

test("routes at most once across follow-ups, agent changes and plugin reloads", async () => {
  const { run, event, calls, records, ctx } = setup()
  await run(event())
  await run(event())
  ctx.session.get = async () => ({ agent: "alloy" })
  await createFirstPromptRouter(ctx)(event())
  assert.equal(calls.generated.length, 1)
  assert.equal(calls.switched.length, 1)
  assert.equal(records.get("routing/v1/ses_test").status, "selected")
})

test("serializes concurrent admissions for the same session", async () => {
  let release
  const wait = new Promise((resolve) => { release = resolve })
  const { run, event, calls } = setup({ generate: async () => {
    await wait
    return { text: JSON.stringify({ model: target, reason: "Enough" }) }
  } })
  const first = run(event())
  const second = run(event())
  release()
  await Promise.all([first, second])
  assert.equal(calls.generated.length, 1)
  assert.equal(calls.switched.length, 1)
})

test("skips children, forks, existing histories and unfinished durable attempts", async () => {
  for (const overrides of [
    { session: { parentID: "ses_parent" } },
    { session: { fork: { sessionID: "ses_original" } } },
    ...["user", "assistant", "synthetic", "compaction", "shell"].map((type) => ({ history: [{ type }] })),
    { records: new Map([["routing/v1/ses_test", { status: "pending" }]]) },
  ]) {
    const { run, event, calls } = setup(overrides)
    await run(event())
    assert.equal(calls.generated.length, 0)
    assert.equal(calls.switched.length, 0)
    assert.equal(calls.listed, 0)
  }
})

test("initial agent/model selection messages do not prevent routing", async () => {
  const { run, event, calls } = setup({ history: [{ type: "agent-switched" }, { type: "model-switched" }] })
  await run(event())
  assert.equal(calls.switched.length, 1)
})

test("missing or disabled routing does nothing", async () => {
  for (const value of [undefined, { enabled: false }]) {
    const { run, event, calls } = setup({ options: { routing: value } })
    await run(event())
    assert.equal(calls.listed, 0)
    assert.equal(calls.generated.length, 0)
  }
})

test("keeps current selection when selected model and variant already match", async () => {
  const { run, event, calls } = setup({ session: { model: { providerID: "mistral", id: "small", variant: "high" } } })
  await run(event())
  assert.equal(calls.generated.length, 1)
  assert.equal(calls.switched.length, 0)
})

test("filters unavailable, disabled, forbidden and unknown-variant candidates internally", async () => {
  const { run, event, calls } = setup({ options: { routing: {
    ...routing,
    candidates: [
      ...routing.candidates,
      { model: "mistral/missing", description: "Unavailable" },
      { model: "mistral/small#unknown", description: "Unknown effort" },
      { model: "openai/other", description: "Forbidden provider" },
      { model: "mistral/disabled", description: "Disabled model" },
    ],
  } }, models: [
    { providerID: "mistral", id: "large", variants: ["medium"] },
    { providerID: "mistral", id: "small", variants: ["high"] },
    { providerID: "openai", id: "other" },
    { providerID: "mistral", id: "disabled", enabled: false },
  ] })
  await run(event())
  assert.equal(calls.switched.length, 1)
  assert.doesNotMatch(calls.generated[0].input.prompt, /missing|unknown|openai\/other|mistral\/disabled/)
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
    assert.equal(records.get("routing/v1/ses_test").status, "fallback")
    assert.doesNotMatch(JSON.stringify(input.metadata), /PRIVATE/)
  }
})

test("timeout releases admission without cancellation and ignores a late selector result", async () => {
  let finish
  const { run, event, calls, records } = setup({
    options: { routing: { ...routing, timeoutMs: 25 } },
    generate: async () => new Promise((resolve) => { finish = resolve }),
  })
  const input = event()
  await completesWithin(() => run(input))
  assert.equal(input.metadata.sessionModelRouting.status, "fallback")
  // No new storage writes are dispatched once the overall budget has expired.
  assert.equal(records.get("routing/v1/ses_test").status, "pending")
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
  ["pending marker write", (ctx, stall) => { ctx.storage.set = () => stall }],
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
    release({ agent: "build", model: { providerID: "openai", id: "initial" } })
    await new Promise((resolve) => setImmediate(resolve))
    assert.equal(calls.switched.length, 0)
  })
}

test("a switch already dispatched at timeout has an uncertain outcome, not a retained-model guarantee", async () => {
  let finish
  let applied = false
  const { ctx, run, event, records } = setup({ options: { routing: { ...routing, timeoutMs: 25 } } })
  ctx.session.switchModel = () => new Promise((resolve) => { finish = () => { applied = true; resolve() } })
  const input = event()
  await completesWithin(() => run(input))
  assert.equal(input.metadata.sessionModelRouting.status, "uncertain")
  assert.equal(records.get("routing/v1/ses_test").status, "pending")
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
    assert.equal(records.get("routing/v1/ses_test").status, "pending")
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

test("preserves a manual model/agent change while the selector is running", async () => {
  for (const latest of [
    { agent: "build", model: { providerID: "openai", id: "manual" } },
    { agent: "plan", model: { providerID: "openai", id: "initial" } },
  ]) {
    const { run, event, calls } = setup({ latest })
    await run(event())
    assert.equal(calls.switched.length, 0)
  }
})

test("invalid configuration or policy retains current model without invoking selector", async () => {
  for (const options of [
    { routing: {} },
    { routing: { ...routing, timeoutMs: -1 } },
    { models: "mistral/*" },
    { models: ["openai/*"] },
    { routing: { ...routing, selector: "mistral/missing" } },
  ]) {
    const { run, event, calls, records } = setup({ options })
    await run(event())
    assert.equal(calls.generated.length, 0)
    assert.equal(calls.switched.length, 0)
    assert.equal(records.get("routing/v1/ses_test").status, "fallback")
  }
})

test("storage failure never blocks prompt admission or starts untracked generation", async () => {
  const { ctx, event, calls } = setup()
  ctx.storage.set = async () => { throw new Error("disk unavailable") }
  await createFirstPromptRouter(ctx)(event())
  assert.equal(calls.generated.length, 0)
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
  assert.equal(records.get("routing/v1/ses_test").status, "pending")
})

test("a variant-only change is applied, and a removed target is rejected after generation", async () => {
  const matchingModel = { providerID: "mistral", id: "small" }
  const first = setup({ session: { model: matchingModel } })
  await first.run(first.event())
  assert.equal(first.calls.switched.length, 1)

  const second = setup()
  const list = second.ctx.model.list
  second.ctx.model.list = async () => second.calls.listed ? [] : list()
  const input = second.event()
  await createFirstPromptRouter(second.ctx)(input)
  assert.equal(second.calls.generated.length, 1)
  assert.equal(second.calls.switched.length, 0)
  assert.equal(input.metadata.sessionModelRouting.status, "fallback")
})

test("accepts fenced JSON and wrapped API responses", async () => {
  const { ctx, event, calls } = setup({ generate: async () => ({ data: {
    text: '```json\n' + JSON.stringify({ model: target, reason: "Enough" }) + '\n```',
  } }) })
  const get = ctx.session.get
  ctx.session.get = async () => ({ data: await get() })
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
