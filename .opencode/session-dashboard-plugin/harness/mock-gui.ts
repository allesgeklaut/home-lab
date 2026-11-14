export function usePlugin() {
  return {
    appearance: {
      mode: () => "dark" as const,
      font: () => "JetBrains Mono, ui-monospace, SFMono-Regular, Menlo, monospace",
    },
    tabs: {
      open: (session: { server: string; id: string }) => {
        console.log("open subagent", session)
      },
    },
    client: {
      session: {
        compact: async (input: { sessionID: string }) => {
          console.log("compact session", input)
          return { id: "cmp_mock", sessionID: input.sessionID }
        },
      },
    },
    toast: {
      show: (toast: {
        title: string
        description: string
        variant: string
      }) => {
        console.log("toast", toast)
      },
    },
  }
}
