import type { MountedSession } from "@opencode/plugin/gui"
import { buildModelFromData } from "../shared/model.js"
import type { DashboardModel, DashboardOptions } from "../shared/types.js"

export function buildGuiModel(
  session: MountedSession,
  options?: DashboardOptions,
): DashboardModel | undefined {
  const data = session.server.data
  if (data.session.missing(session.id)) return undefined
  return buildModelFromData(data, session.id, options)
}

export async function syncGuiSession(session: MountedSession): Promise<void> {
  const data = session.server.data
  await Promise.allSettled([
    data.session.sync(session.id),
    data.session.message.sync(session.id),
    data.location.model.sync(),
  ])

  const children = data.session.family(session.id).filter((id) => id !== session.id)
  await Promise.allSettled(
    children.flatMap((id) => [
      data.session.sync(id),
      data.session.message.sync(id),
    ]),
  )
}
