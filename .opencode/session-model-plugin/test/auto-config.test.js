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
  assert.equal(config.model, "mistral/auto")
  assert.ok(!config.model.includes("#"), "root model cannot retain a variant")

  const models = config.providers.mistral.models
  const auto = models.auto
  assert.ok(auto, "expected the Auto alias")
  assert.equal(auto.modelID, "zai-glm-5-3-flash")
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

test("routing options stay concrete and exclude Auto", async () => {
  const options = pluginOptions(await readJson("opencode.json"))
  assert.deepEqual(options.agents, ["alloy", "build", "plan"])
  assert.deepEqual(options.models, ["mistral/*"])
  assert.equal(options.routing.selector, "mistral/mistral-large-4#medium")
  assert.equal(options.routing.timeoutMs, 45000)
  assert.deepEqual(
    options.routing.candidates.map((candidate) => candidate.model),
    [
      "mistral/zai-glm-5-3-flash#high",
      "mistral/zai-glm-5-3-flash#max",
      "mistral/deepseek-v4-1-flash-1017#high",
      "mistral/deepseek-v4-1-flash-1017#max",
      "mistral/mistral-large-4#medium",
      "mistral/mistral-large-4#high",
      "mistral/zai-glm-5-3#high",
      "mistral/zai-glm-5-3#max",
      "mistral/zai-glm-5-3-longctx#max",
    ],
  )
  assert.ok(options.routing.candidates.every((candidate) => !candidate.model.startsWith("mistral/auto")))
  assert.ok(
    options.routing.candidates.some((candidate) => candidate.model.startsWith("mistral/zai-glm-5-3-flash#")),
    "the Auto fallback model must remain in the shared shortlist",
  )
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
    model: { list: async () => [{ providerID: "mistral", id: "zai-glm-5-3-flash", variants: ["high"] }] },
    session: {
      get: async () => ({ model: { providerID: "mistral", id: "auto", variant: "default" } }),
      switchModel: async (input) => void switched.push(input),
    },
  }
  const run = (agent) => createSwitchModelTool(ctx).execute(
    { model: "mistral/zai-glm-5-3-flash#high", reason: "Config guard test" },
    { agent, sessionID: "ses_config" },
  )
  for (const agent of options.agents) await run(agent)
  assert.equal(switched.length, options.agents.length)
  for (const tier of ["tier-1", "tier-2", "tier-3", "tier-4"]) {
    await assert.rejects(run(tier), new RegExp(`not enabled for the ${tier} agent`))
  }
  await assert.rejects(run("reviewer"), /authorized Auto handoff/)
  assert.equal(switched.length, options.agents.length)
})

test("tier workers keep concrete pinned models", async () => {
  const expected = {
    "tier-1.md": "mistral/deepseek-v4-1-flash-1017#max",
    "tier-2.md": "mistral/mistral-large-4#high",
    "tier-3.md": "mistral/zai-glm-5-3#high",
    "tier-4.md": "mistral/zai-glm-5-3#max",
  }
  for (const [file, model] of Object.entries(expected)) {
    const text = await read(`.opencode/agents/${file}`)
    const escaped = model.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")
    assert.match(frontmatter(text), new RegExp(`^model: ${escaped}$`, "m"))
  }
})

test("reviewer is an Auto subagent with a deny-all, read-only surface", async () => {
  const text = await read(".opencode/agents/reviewer.md")
  const fm = frontmatter(text)
  assert.match(fm, /^mode: subagent$/m)
  assert.match(fm, /^model: mistral\/auto$/m)

  const rules = permissions(fm)
  assert.deepEqual(rules[0], { action: "*", resource: "*", effect: "deny" })
  for (const action of ["read", "glob", "grep", "get_session_model", "switch_model"]) {
    assert.ok(
      rules.some((rule) => rule.action === action && rule.resource === "*" && rule.effect === "allow"),
      `expected ${action} to be allowed`,
    )
  }
  for (const action of ["edit", "shell", "execute", "subagent", "question", "webfetch", "websearch", "skill"]) {
    assert.ok(
      !rules.some((rule) => rule.action === action && rule.effect === "allow"),
      `expected ${action} not to be allowed`,
    )
  }
  const readAllow = rules.findIndex(
    (rule) => rule.action === "read" && rule.resource === "*" && rule.effect === "allow",
  )
  for (const resource of [
    "/opt/secrets/*",
    "~/.ssh/*",
    "**/.env",
    "**/.env.*",
    "**/.envrc",
    "**/*.pem",
    "**/credentials.json",
  ]) {
    const index = rules.findIndex(
      (rule) => rule.action === "read" && rule.resource === resource && rule.effect === "deny",
    )
    assert.ok(index > readAllow, `expected reads of ${resource} to be denied after the general allow`)
  }
  assert.match(text, /get_session_model/)
  assert.match(text, /not a filesystem or network sandbox/i)
})

test("Alloy may launch the reviewer after the blanket subagent deny", async () => {
  const rules = permissions(frontmatter(await read(".opencode/agents/alloy.md")))
  const deny = rules.findIndex((rule) => rule.action === "subagent" && rule.resource === "*" && rule.effect === "deny")
  const reviewer = rules.findIndex(
    (rule) => rule.action === "subagent" && rule.resource === "reviewer" && rule.effect === "allow",
  )
  assert.notEqual(deny, -1)
  assert.ok(reviewer > deny, "reviewer allow must follow the blanket deny")
  const tier4 = rules.findIndex(
    (rule) => rule.action === "subagent" && rule.resource === "tier-4" && rule.effect === "allow",
  )
  assert.ok(reviewer > tier4, "reviewer allow must follow the tier allows")
  for (const tier of ["tier-1", "tier-2", "tier-3", "tier-4"]) {
    assert.ok(
      rules.some((rule) => rule.action === "subagent" && rule.resource === tier && rule.effect === "allow"),
      `expected ${tier} to remain allowed`,
    )
  }
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
})
