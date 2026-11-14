import { boundaryEnd } from "./context.js"
import type { SessionMessage, SpeedSummary } from "./types.js"

/**
 * Session-average generation and prefill throughput. Generation uses the
 * streamed-to-first-token window when the host reports it, otherwise the
 * created-to-streamed window (the same fallback OpenCode uses). Prefill needs
 * an explicit first-token timestamp, so it stays undefined when the host does
 * not provide one.
 */
export function speedSummary(
  messages: ReadonlyArray<SessionMessage>,
  boundary?: string,
): SpeedSummary {
  const end = boundaryEnd(messages, boundary)
  if (end === undefined) return {}

  let generated = 0
  let generatedMs = 0
  let prefilled = 0
  let prefilledMs = 0

  for (let index = 0; index < end; index += 1) {
    const message = messages[index]
    if (message.type !== "assistant" || message.tokens === undefined) continue

    const { created, firstToken, streamed } = message.time
    if (streamed !== undefined) {
      const duration = streamed - (firstToken ?? created)
      const tokens = message.tokens.output + message.tokens.reasoning
      if (duration > 0 && tokens > 0) {
        generated += tokens
        generatedMs += duration
      }
    }

    if (firstToken !== undefined) {
      const duration = firstToken - created
      const tokens =
        message.tokens.input +
        message.tokens.cache.read +
        message.tokens.cache.write
      if (duration > 0 && tokens > 0) {
        prefilled += tokens
        prefilledMs += duration
      }
    }
  }

  return {
    generation: generatedMs > 0 ? generated / (generatedMs / 1000) : undefined,
    prefill: prefilledMs > 0 ? prefilled / (prefilledMs / 1000) : undefined,
  }
}
