-- ============================================================================
-- Store.nvim Plugin
-- Plugin browser and installer
-- ============================================================================

return {
  "alex-popov-tech/store.nvim",
  dependencies = {
    "OXY2DEV/markview.nvim",
  },
  cmd = "Store",
  opts = {},
  keys = {
    { "<leader>ps", "<cmd>Store<cr>", desc = "Plugin Store" },
  },
}
