-- ============================================================================
-- snacks.nvim
-- Minimal install: only input + picker modules to enhance opencode.nvim.
-- Deliberately NOT enabling explorer/indent/git/dashboard/etc. to avoid
-- colliding with nvim-tree, indent-blankline, gitsigns, bufferline, etc.
-- ============================================================================

return {
  "folke/snacks.nvim",
  priority = 1000,
  lazy = false,
  dependencies = {
    "nickjvandyke/opencode.nvim",
  },
  opts = {
    -- Enhances opencode.nvim's Ask() input UI
    input = {
      enabled = true,
    },

    -- Enhances opencode.nvim's Select() picker UI.
    -- Coexists with Telescope (we don't remap <leader>ff etc. here).
    picker = {
      enabled = true,
      win = {
        input = {
          keys = {
            ["<a-o>"] = { "opencode_send", mode = { "n", "i" } },
          },
        },
      },
      actions = {
        -- Send selected picker items (files/lines) as context to OpenCode.
        opencode_send = function(picker)
          local items = vim.tbl_map(function(item)
            return item.file
                and require("opencode").format({ path = item.file, from = item.pos, to = item.end_pos })
                or item.text
          end, picker:selected({ fallback = true }))
          require("opencode").prompt(table.concat(items, ", ") .. " ")
        end,
      },
    },
  },
}