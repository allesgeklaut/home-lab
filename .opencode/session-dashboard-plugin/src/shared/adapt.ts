import type {
  ModelInfo as ClientModelInfo,
  SessionInfo as ClientSessionInfo,
  SessionMessageInfo as ClientSessionMessageInfo,
} from "@opencode/client"
import type { ModelInfo, SessionInfo, SessionMessage } from "./types.js"

export function adaptSession(info: ClientSessionInfo): SessionInfo {
  return {
    id: info.id,
    parentID: info.parentID,
    title: info.title,
    agent: info.agent,
    model: info.model,
    cost: info.cost,
    tokens: info.tokens,
    time: info.time,
    revert: info.revert ? { messageID: info.revert.messageID } : undefined,
    outcome: info.outcome,
  }
}

export function adaptMessage(message: ClientSessionMessageInfo): SessionMessage {
  const value = message as unknown as {
    id: string
    type: string
    time: {
      created: number
      firstToken?: number
      streamed?: number
      completed?: number
    }
    model?: SessionMessage["model"]
    tokens?: SessionMessage["tokens"]
    cost?: number
    status?: string
  }

  return {
    id: value.id,
    type: value.type,
    time: {
      created: value.time.created,
      firstToken: value.time.firstToken,
      streamed: value.time.streamed,
      completed: value.time.completed,
    },
    model: value.model,
    tokens: value.tokens,
    cost: value.cost,
    status: value.status,
  }
}

export function adaptModel(model: ClientModelInfo): ModelInfo {
  return {
    id: model.id,
    modelID: model.modelID,
    providerID: model.providerID,
    name: model.name,
    limit: model.limit,
    variants: model.variants,
    settings: model.settings as ModelInfo["settings"],
  }
}

export function adaptModels(models: ReadonlyArray<ClientModelInfo>): ModelInfo[] {
  return models.map(adaptModel)
}
