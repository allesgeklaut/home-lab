import test from "node:test"
import assert from "node:assert/strict"
import { readFile } from "node:fs/promises"
import { createSwitchModelTool } from "../switch-model.js"

const root = new URL("../../../", import.meta.url)
const read = (path) => readFile(new URL(path, root), "utf8")
const readJson = async (path) => JSON.parse(await read(path))

function frontmatter(text) {
  const match = /^---\r?\n([\s\S]*?)\r?\n---/.exec(text)
  assert.ok(match, "expected YAML frontmatter")
  return match[1]
}

function unquote(value) {
  const trimmed = value.trim()
  return /^(["']).*\1$/.test(trimmed) ? trimmed.slice(1, -1) : trimmed
}

function permissions(frontmatterText) {
  const lines = frontmatterText.split(/\r?\n/)
  const start = lines.indexOf("permissions:")
  assert.notEqual(start, -1, "expected permissions")
  const rules = []
  let rule
  for (const line of lines.slice(start + 1)) {
    if (!line.startsWith("  ")) break
    const action = /^  - action: (.+)$/.exec(line)
    if (action) {
      rule = { action: unquote(action[1]) }
      rules.push(rule)
      continue
    }
    const field = /^    (resource|effect): (.+)$/.exec(line)
    if (field && rule) rule[field[1]] = unquote(field[2])
  }
  return rules
}

function pluginOptions(config) {
  const plugin = config.plugins.find(
    (entry) => typeof entry === "object" && entry.package === "/opt/stacks/.opencode/session-model-plugin",
  )
  assert.ok(plugin, "expected the session-model plugin entry")
  return plugin.options
}

test("root model selects the Auto alias, which maps to a real configured fallback", async () => {
  const config = await readJson("opencode.json")
  assert.equal(config.model, "opencode-go/auto")
  assert.ok(!config.model.includes("#"), "root model cannot retain a variant")

  const models = config.providers["opencode-go"].models
  const auto = models.auto
  assert.ok(auto, "expected the Auto alias")
  assert.equal(auto.modelID, "deepseek-v4.1-flash")
  assert.equal(config.providers.mistral.models.auto, undefined, "the retired Mistral Auto alias must stay gone")
  assert.notEqual(auto.modelID, "auto")
  assert.equal(auto.name, "Auto (routed)")
  assert.equal(auto.limit?.input, 307200)
  assert.notEqual(auto.disabled, true)
  assert.equal(auto.capabilities, undefined, "capabilities inherit from the mapped model")
  assert.equal(auto.variants, undefined, "variants inherit from the mapped model")

  const fallback = models[auto.modelID]
  assert.ok(fallback, "fallback model must already be configured")
  assert.notEqual(fallback.disabled, true)
})

test("title agent runs on Big Pickle, and the retired provider deny stays gone", async () => {
  const config = await readJson("opencode.json")
  assert.equal(config.agents?.title?.model, "opencode/big-pickle")
  assert.ok(
    !(config.policies ?? []).some((rule) => rule.resource === "opencode"),
    "the OpenCode provider deny must stay gone",
  )
})

test("the Hy4 shortlist step is defined in the provider config with explicit limits", async () => {
  const config = await readJson("opencode.json")
  const hy4 = config.providers["opencode-go"].models.hy4
  assert.ok(hy4, "hy4 must be defined in the opencode-go provider config")
  assert.ok(hy4.limit, "hy4 needs explicit limits; it is not in the installed catalog")
  assert.equal(hy4.limit?.input, 307200, "hy4 input must match the shared 300K session cap")
  assert.ok(
    hy4.variants?.some((variant) => variant.id === "high"),
    "hy4 needs an explicit high variant for the shortlist and the tier-2 pin",
  )
})

test("routing options stay concrete and exclude Auto", async () => {
  const options = pluginOptions(await readJson("opencode.json"))
  assert.deepEqual(options.agents, ["build", "plan"])
  assert.deepEqual(options.models, ["opencode-go/*"])
  assert.equal(options.routing.selector, "opencode-go/deepseek-v4.1-flash#high")
  assert.equal(options.routing.timeoutMs, 45000)
  assert.deepEqual(
    options.routing.candidates.map((candidate) => candidate.model),
    [
      "opencode-go/deepseek-v4.1-flash#high",
      "opencode-go/deepseek-v4.1-flash#max",
      "opencode-go/hy4#high",
      "opencode-go/glm-5.3#high",
      "opencode-go/glm-5.3#max",
      "opencode-go/glm-5.3-longctx#max",
    ],
  )
  assert.ok(options.routing.candidates.every((candidate) => !candidate.model.includes("kimi")),
    "Kimi K2.7 Code was retired from the shortlist: weaker than DeepSeek Flash on the AA index")
  assert.ok(options.routing.candidates.every((candidate) => !candidate.model.startsWith("opencode-go/auto")))
  assert.ok(
    options.routing.candidates.every((candidate) => !candidate.model.startsWith("opencode-go/glm-5.3-flash#")),
    "the slow GLM 5.3 Flash model is deliberately excluded from the routing shortlist",
  )
  assert.ok(
    options.routing.candidates.some((candidate) => candidate.model.startsWith("opencode-go/deepseek-v4.1-flash#")),
    "the Auto fallback model must remain in the shared shortlist",
  )
})

test("the long-context alias maps to GLM-5.3 with a raised input cap", async () => {
  const config = await readJson("opencode.json")
  const longctx = config.providers["opencode-go"].models["glm-5.3-longctx"]
  assert.ok(longctx, "expected the long-context alias")
  assert.equal(longctx.modelID, "glm-5.3")
  assert.ok(longctx.limit?.input > 307200, "the long-context cap must exceed the standard 300K session cap")
  assert.ok(config.providers["opencode-go"].models[longctx.modelID], "the mapped model must be configured")
  assert.notEqual(longctx.disabled, true)
})

test("unused GPT image generation plugin is not configured", async () => {
  const config = await readJson("opencode.json")
  assert.ok(config.plugins.every((entry) =>
    (typeof entry === "string" ? entry : entry.package) !== "@makcimbx/opencode-gpt-imagegen"))
})

test("manual switching with the configured options reaches only primary agents", async () => {
  const options = pluginOptions(await readJson("opencode.json"))
  const switched = []
  const ctx = {
    options,
    model: { list: async () => [{ providerID: "opencode-go", id: "glm-5.3-flash", variants: ["high"] }] },
    session: {
      get: async () => ({ model: { providerID: "opencode-go", id: "auto", variant: "default" } }),
      switchModel: async (input) => void switched.push(input),
    },
  }
  const run = (agent) => createSwitchModelTool(ctx).execute(
    { model: "opencode-go/glm-5.3-flash#high", reason: "Config guard test" },
    { agent, sessionID: "ses_config" },
  )
  for (const agent of options.agents) await run(agent)
  assert.equal(switched.length, options.agents.length)
  for (const tier of ["tier-0", "tier-1", "tier-2", "tier-3", "tier-4"]) {
    await assert.rejects(run(tier), new RegExp(`not enabled for the ${tier} agent`))
  }
  await assert.rejects(run("reviewer"), /authorized Auto handoff/)
  assert.equal(switched.length, options.agents.length)
})

test("tier workers keep concrete pinned models", async () => {
  const expected = {
    "tier-0.md": "llama-cpp/qwen3.8-flash-next",
    "tier-1.md": "opencode-go/deepseek-v4.1-flash#max",
    "tier-2.md": "opencode-go/hy4#high",
    "tier-3.md": "opencode-go/glm-5.3#high",
    "tier-4.md": "opencode-go/glm-5.3#max",
  }
  for (const [file, model] of Object.entries(expected)) {
    const text = await read(`.opencode/agents/${file}`)
    const escaped = model.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")
    assert.match(frontmatter(text), new RegExp(`^model: ${escaped}$`, "m"))
  }
})

test("tier 0 stays local and is reserved for explicit user requests", async () => {
  const text = await read(".opencode/agents/tier-0.md")
  const fm = frontmatter(text)
  assert.match(fm, /local/i)
  assert.match(fm, /Only assign when the user explicitly asks/i)
  assert.match(fm, /otherwise rely on the other tiers/)
  await assert.rejects(read(".opencode/agent/qwen-worker.md"), (error) => error.code === "ENOENT")
})

test("shared permissions ask before secret file reads and preserve hard-denied directories", async () => {
  const { permissions: rules } = await readJson("opencode.json")
  const prompts = [
    "*.env", "*.env.*", "*.envrc", "*.pem", "*.key",
    "*credentials.json", "*credentials.yml", "*credentials.yaml", "*.credentials*",
  ].map((resource) => {
    const index = rules.findIndex(
      (rule) => rule.action === "read" && rule.resource === resource && rule.effect === "ask",
    )
    assert.notEqual(index, -1, `expected an approval prompt for ${resource}`)
    return index
  })
  for (const resource of ["/opt/secrets/*", "~/.ssh/*"]) {
    for (const action of ["read", "edit"]) {
      const index = rules.findIndex(
        (rule) => rule.action === action && rule.resource === resource && rule.effect === "deny",
      )
      assert.ok(index > Math.max(...prompts), `expected ${action} deny for ${resource} after prompts`)
    }
  }
})

test("reviewer is an Auto subagent inheriting shared permissions for verification", async () => {
  const text = await read(".opencode/agents/reviewer.md")
  const fm = frontmatter(text)
  assert.match(fm, /^mode: subagent$/m)
  assert.match(fm, /^model: opencode-go\/auto$/m)

  assert.doesNotMatch(fm, /^permissions?:/m, "reviewer must not override inherited permissions")
  const config = await readJson("opencode.json")
  assert.equal(config.agents?.reviewer?.permissions, undefined)
  assert.match(text, /Use shell commands for Git inspection, unit tests/)
  assert.match(text, /Normal generated test\/build artifacts are permitted/)
  assert.match(text, /do not modify source or\nconfiguration/)
  assert.match(text, /plugin rejects manual\nreviewer switches/)
  assert.match(text, /get_session_model/)
  assert.match(text, /not a filesystem or network sandbox/i)
})

test("the retired Alloy primary agent stays gone", async () => {
  await assert.rejects(read(".opencode/agents/alloy.md"), (error) => error.code === "ENOENT")
})

test("review skill launches the shared Auto reviewer without a model override", async () => {
  const text = await read(".opencode/skills/review/SKILL.md")
  assert.match(frontmatter(text), /^description: .*Auto/m)
  assert.match(text, /routing\.selector/)
  assert.match(text, /routing\.candidates/)
  assert.match(text, /`reviewer`/)
  assert.match(text, /pass a model parameter/)
  assert.doesNotMatch(text, /normally `tier-1`/)
  assert.doesNotMatch(text, /Use a per-call model override/)
  assert.match(text, /use shell commands for Git\ninspection, unit tests/)
  assert.match(text, /Normal generated test\/build artifacts are permitted/)
  assert.doesNotMatch(text, /reviewer (?:has no shell|cannot run git)/)
})
