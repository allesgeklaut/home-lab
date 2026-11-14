import { createEffect, createMemo, createSignal, onCleanup } from "solid-js"
import type { Accessor } from "solid-js"
import type { Context } from "@opencode/plugin/tui/context"
import type { DashboardModel, DashboardOptions } from "../shared/types.js"
import { buildTuiModel, syncTuiSession } from "./data.js"

// Keep this reactive helper in .tsx: OpenCode's TUI loader rewrites Solid
// imports there to the host runtime. A separately resolved Solid instance in
// a .ts helper can update its model without notifying the rendered view.

export type DashboardModelState = {
  model: Accessor<DashboardModel | undefined>
  now: Accessor<number>
  lastUpdate: Accessor<number>
  refresh: () => void
}

/**
 * Shared reactive model for the TUI panel, sidebar section, and footer.
 * Rebuilds on host session events and periodically even while idle, so a
 * stale initial status cannot prevent the dashboard from catching up.
 */
export function useDashboardModel(
  context: Context,
  sessionID: Accessor<string>,
): DashboardModelState {
  const [tick, setTick] = createSignal(0)
  const [now, setNow] = createSignal(Date.now())
  const [lastUpdate, setLastUpdate] = createSignal(Date.now())
  const options = () => (context.options ?? {}) as DashboardOptions

  const model = createMemo(() => {
    tick()
    const id = sessionID()
    const session = context.data.session.get(id)
    // Track the reactive collections the model reads. The session can mount
    // before its messages and models arrive; without these reads the memo
    // would keep returning the empty first snapshot.
    context.data.session.message.list(id)
    context.data.session.family(id)
    context.data.location.model.list(session?.location)
    return buildTuiModel(context, id)
  })

  const bump = () => {
    setNow(Date.now())
    setTick((value) => value + 1)
  }

  const refresh = () => {
    void syncTuiSession(context, sessionID())
      .then(() => {
        setLastUpdate(Date.now())
        bump()
      })
      .catch(() => undefined)
  }

  createEffect(() => {
    const id = sessionID()
    void syncTuiSession(context, id)
      .then(() => {
        setLastUpdate(Date.now())
        setTick((value) => value + 1)
      })
      .catch(() => undefined)
  })

  const stop = context.data.listen(({ details }) => {
    if (!details.type.startsWith("session.")) return
    if (details.type.endsWith(".delta") || details.type.endsWith(".streamed")) return
    setLastUpdate(Date.now())
    bump()
  })
  onCleanup(stop)

  const timer = setInterval(
    () => {
      bump()
    },
    Math.max(500, options().refreshMs ?? 1000),
  )
  onCleanup(() => clearInterval(timer))

  return { model, now, lastUpdate, refresh }
}
