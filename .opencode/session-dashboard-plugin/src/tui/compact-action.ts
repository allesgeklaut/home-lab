import type { Context } from "@opencode/plugin/tui/context"

/**
 * Request compaction for one session. This is the plugin's only write action.
 * The caller owns the in-flight signal so each surface can disable its own
 * control while the request is pending.
 */
export function requestCompaction(
  context: Context,
  sessionID: string,
  setCompacting: (value: boolean) => void,
): void {
  if (typeof context.client?.session?.compact !== "function") {
    context.ui.toast?.show?.({
      title: "Session dashboard",
      message: "Compaction API is unavailable in this OpenCode version.",
      variant: "error",
    })
    return
  }

  setCompacting(true)
  void context.client.session
    .compact({ sessionID })
    .then(() => {
      context.ui.toast?.show?.({
        title: "Session dashboard",
        message: "Compaction requested",
        variant: "success",
      })
    })
    .catch((error) => {
      context.ui.toast?.show?.({
        title: "Session dashboard",
        message: `Compaction failed: ${
          error instanceof Error ? error.message : String(error)
        }`,
        variant: "error",
      })
    })
    .finally(() => setCompacting(false))
}
