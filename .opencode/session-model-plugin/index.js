import { createSwitchModelTool } from "./switch-model.js"
import { createGetSessionModelTool } from "./get-session-model.js"
import { createFirstPromptRouter } from "./first-prompt-router.js"
import { createRoutingInstruction } from "./routing-handoff.js"

export default {
  id: "session-model",
  async setup(ctx) {
    await ctx.tool.transform((editor) => {
      editor.add(createSwitchModelTool(ctx))
      editor.add(createGetSessionModelTool(ctx))
    })
    if (ctx.options?.routing && ctx.options.routing.enabled !== false) {
      await ctx.session.hook("prompt", createFirstPromptRouter(ctx))
      await ctx.session.hook("context", createRoutingInstruction(ctx))
    }
  },
}
