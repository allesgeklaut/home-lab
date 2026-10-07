-- ============================================================================
-- Dracula Colorscheme Plugin
-- ============================================================================

return {
  "Mofiqul/dracula.nvim",
  lazy = false,
  priority = 1000,
  config = function()
    require("dracula").setup({
      -- Customize the color palette
      transparent_bg = false,
      italic_comment = true,

      -- Overrides the default highlights
      overrides = {},

      -- Colors can be customized
      colors = {},
    })

    -- Load the colorscheme
    vim.cmd([[colorscheme dracula]])
  end,
}
