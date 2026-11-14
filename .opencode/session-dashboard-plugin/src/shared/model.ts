import type {
  ModelInfo as ClientModelInfo,
  SessionInfo as ClientSessionInfo,
  SessionMessageInfo as ClientSessionMessageInfo,
} from "@opencode/client"
import { adaptMessage, adaptModel, adaptSession } from "./adapt.js"
import { buildDashboardModel } from "./sessions.js"
import type { DashboardModel, DashboardOptions, ModelInfo, SessionInfo } from "./types.js"

/** The reactive data subset shared by the TUI and GUI hosts. */
export interface DashboardDataSource {
  readonly session: {
    get(sessionID: string): ClientSessionInfo | undefined
    root(sessionID: string): string
    family(sessionID: string): string[]
    status(sessionID: string): "idle" | "running"
    readonly message: {
      list(sessionID: string): ClientSessionMessageInfo[]
    }
  }
  readonly location: {
    readonly model: {
      list(): ClientModelInfo[] | undefined
    }
  }
}

export function buildModelFromData(
  data: DashboardDataSource,
  sessionID: string,
  options?: DashboardOptions,
): DashboardModel | undefined {
  const raw = data.session.get(sessionID)
  if (!raw || !raw.id) return undefined

  const session = adaptSession(raw)
  const sessions = familySessions(data, sessionID)
  if (!sessions.some((value) => value.id === sessionID)) sessions.push(session)

  const models = new Map<string, ModelInfo>()
  for (const value of data.location.model.list() ?? []) {
    const model = adaptModel(value)
    models.set(modelKey(model.providerID, model.id), model)
  }

  return buildDashboardModel({
    session,
    status: data.session.status(sessionID),
    sessions,
    model: (ref) => (ref ? models.get(modelKey(ref.providerID, ref.id)) : undefined),
    statusOf: (id) => data.session.status(id),
    messages: (id) => data.session.message.list(id).map(adaptMessage),
    options,
  })
}

function familySessions(data: DashboardDataSource, sessionID: string): SessionInfo[] {
  const ids = new Set<string>([sessionID, data.session.root(sessionID)])
  for (const id of data.session.family(sessionID)) ids.add(id)

  return [...ids]
    .map((id) => data.session.get(id))
    .filter((value): value is NonNullable<typeof value> => value !== undefined && Boolean(value.id))
    .map(adaptSession)
}

function modelKey(providerID: string, id: string): string {
  return `${providerID}/${id}`
}
