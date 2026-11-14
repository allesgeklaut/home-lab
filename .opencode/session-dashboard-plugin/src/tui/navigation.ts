import type { Context } from "@opencode/plugin/tui/context"

/**
 * Navigate straight to a session route.
 *
 * Do not route this through `context.ui.tabs.focus()`. The host implements
 * that as `sessionTabs.select()`, which resolves the id through
 * `session.root()` and returns true whenever tabs are enabled. For a subagent
 * that navigates back to the parent session and suppresses the router call,
 * so the row looks unclickable.
 */
export function openSession(context: Context, sessionID: string) {
  context.ui.router.navigate({ type: "session", sessionID })
}
