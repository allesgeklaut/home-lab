import { Plugin } from "@opencode/plugin"

/**
 * The dashboard runs entirely in the TUI and GUI parts and only reads session
 * data, except when the user explicitly requests compaction from the panel.
 * This server part exists only to give the package a stable plugin id, so the
 * GUI panel key (`session-dashboard:dashboard`) does not depend on the package
 * name or the directory it is installed in.
 */
export default Plugin.define({
  id: "session-dashboard",
  setup() {},
})
