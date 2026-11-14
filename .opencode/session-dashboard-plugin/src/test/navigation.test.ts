import assert from "node:assert/strict"
import test from "node:test"
import { openSession } from "../tui/navigation.js"

test("openSession navigates to the exact session without tabs.focus", () => {
  const routes: Array<{ type: string; sessionID: string }> = []
  const context = {
    ui: {
      router: {
        navigate: (route: { type: string; sessionID: string }) => {
          routes.push(route)
        },
      },
      tabs: {
        enabled: () => true,
        // The host resolves tabs.focus() through session.root(), which would
        // navigate a subagent click back to its parent. It must never be used.
        focus: () => {
          throw new Error("tabs.focus must not be used for subagent navigation")
        },
      },
    },
  } as unknown as Parameters<typeof openSession>[0]

  openSession(context, "ses_child")

  assert.deepEqual(routes, [{ type: "session", sessionID: "ses_child" }])
})
