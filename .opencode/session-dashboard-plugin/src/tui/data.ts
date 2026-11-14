import type { Context } from "@opencode/plugin/tui/context"
import { buildModelFromData } from "../shared/model.js"
import type { DashboardModel, DashboardOptions } from "../shared/types.js"

export function buildTuiModel(
  context: Context,
  sessionID: string,
): DashboardModel | undefined {
  return buildModelFromData(
    context.data,
    sessionID,
    (context.options ?? {}) as DashboardOptions,
  )
}

export async function syncTuiSession(context: Context, sessionID: string): Promise<void> {
  await Promise.allSettled([
    context.data.session.sync(sessionID),
    context.data.session.message.sync(sessionID),
    context.data.location.model.sync(),
  ])

  const children = context.data.session
    .family(sessionID)
    .filter((id) => id !== sessionID)

  await Promise.allSettled(
    children.flatMap((id) => [
      context.data.session.sync(id),
      context.data.session.message.sync(id),
    ]),
  )
}
