import { createSwitchModelTool } from "./switch-model.js"
import { createGetSessionModelTool } from "./get-session-model.js"

export default {
  id: "session-model",
  async setup(ctx) {
    await ctx.tool.transform((editor) => {
      editor.add(createSwitchModelTool(ctx))
      editor.add(createGetSessionModelTool(ctx))
    })
  },
}
