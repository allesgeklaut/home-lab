import { createGetSessionCostTool } from "./session-cost.js"

export default {
  id: "session-cost",
  async setup(ctx) {
    await ctx.tool.transform((editor) => {
      editor.add(createGetSessionCostTool(ctx))
    })
  },
}
