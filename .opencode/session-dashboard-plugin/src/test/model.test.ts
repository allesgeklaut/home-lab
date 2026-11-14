import assert from "node:assert/strict"
import test from "node:test"
import type { DashboardDataSource } from "../shared/model.js"
import { buildModelFromData } from "../shared/model.js"

const rawSession = {
  id: "ses_1",
  projectID: "proj_1",
  agent: "build",
  model: { providerID: "local", id: "qwen" },
  cost: 0,
  tokens: { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } },
  time: { created: 1, updated: 2 },
  location: { directory: "/work" },
  title: "Test session",
}

const messages = [
  {
    id: "msg_1",
    type: "assistant",
    time: { created: 1_000, firstToken: 1_200, streamed: 2_200 },
    tokens: {
      input: 1_000,
      output: 100,
      reasoning: 0,
      cache: { read: 5_000, write: 0 },
    },
  },
]

let requestedLocation: unknown

const data = {
  session: {
    get: (id: string) => (id === "ses_1" ? rawSession : undefined),
    root: () => "ses_1",
    family: () => ["ses_1"],
    status: () => "idle",
    message: { list: () => messages },
  },
  location: {
    model: {
      list: (location?: unknown) => {
        requestedLocation = location
        return [
          {
            id: "qwen",
            modelID: "qwen",
            providerID: "local",
            name: "Qwen",
            limit: { context: 100_000, output: 8_000 },
          },
        ]
      },
    },
  },
} as unknown as DashboardDataSource

test("buildModelFromData uses the session location and live timings", () => {
  const model = buildModelFromData(data, "ses_1")

  assert.deepEqual(requestedLocation, { directory: "/work" })
  assert.equal(model?.context.limit, 100_000)
  assert.equal(model?.context.used, 6_000)
  assert.equal(model?.speed.generation, 100)
  assert.equal(model?.speed.prefill, 5_000)
})
