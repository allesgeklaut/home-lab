-- ============================================================================
-- LSP Configuration
-- Language Server Protocol setup for Python and other languages
-- ============================================================================

return {
	"neovim/nvim-lspconfig",
	event = { "BufReadPre", "BufNewFile" },
	config = function()
		local lspconfig = vim.lsp.config
		require("mason").setup({
			registries = { "github:crashdummyy/mason-registry", "github:mason-org/mason-registry" },
		})
		require("mason-lspconfig").setup()
		require("roslyn").setup()
		require("blink.cmp").setup({
			completion = {
				documentation = { auto_show = true },
			},
			keymap = {
				preset = "none",
				["<C-j>"] = { "select_next", "fallback" },
				["<C-k>"] = { "select_prev", "fallback" },
				["<CR>"] = { "accept", "fallback" },
			},
			-- Show LSP (and buffer fallback) completions in opencode.nvim's Ask input.
			-- Per opencode.nvim README: only applicable when using snacks.input.
			-- Only adds the opencode_ask filetype; global defaults are left untouched.
			sources = {
				per_filetype = {
					opencode_ask = { "lsp", "buffer" },
				},
				providers = {
					lsp = { fallbacks = {} }, -- show buffer completions when no LSP completions
				},
			},
		})

		vim.diagnostic.config({
			signs = {
				numhl = {
					[vim.diagnostic.severity.ERROR] = "DiagnosticSignError",
					[vim.diagnostic.severity.HINT] = "DiagnosticSignHint",
					[vim.diagnostic.severity.INFO] = "DiagnosticSignInfo",
					[vim.diagnostic.severity.WARN] = "DiagnosticSignWarn",
				},
				text = {
					[vim.diagnostic.severity.ERROR] = "X",
					[vim.diagnostic.severity.HINT] = "?",
					[vim.diagnostic.severity.INFO] = "I",
					[vim.diagnostic.severity.WARN] = "!",
				},
			},
			update_in_insert = true,
			virtual_text = false,
			virtual_lines = { current_line = true },
		})

		-- LSP keymaps
		local on_attach = function(client, bufnr)
			local opts = { noremap = true, silent = true, buffer = bufnr }

			-- Key mappings for LSP
			vim.keymap.set("n", "gd", vim.lsp.buf.definition, opts)
			vim.keymap.set("n", "gD", vim.lsp.buf.declaration, opts)
			vim.keymap.set("n", "gr", vim.lsp.buf.references, opts)
			vim.keymap.set("n", "gi", vim.lsp.buf.implementation, opts)
			vim.keymap.set("n", "K", vim.lsp.buf.hover, opts)
			vim.keymap.set("n", "<leader>rn", vim.lsp.buf.rename, opts)
			vim.keymap.set("n", "<leader>ca", vim.lsp.buf.code_action, opts)
			vim.keymap.set("n", "<leader>f", function()
				vim.lsp.buf.format({ async = true })
			end, opts)
			vim.keymap.set("n", "[d", vim.diagnostic.goto_prev, opts)
			vim.keymap.set("n", "]d", vim.diagnostic.goto_next, opts)
			vim.keymap.set("n", "<leader>q", vim.diagnostic.setloclist, opts)
			vim.keymap.set("n", "<leader>d", vim.diagnostic.open_float, opts)
		end
	end,
	dependencies = {
		"seblyng/roslyn.nvim",
		"mason-org/mason-lspconfig.nvim",
		"mason-org/mason.nvim",
		{ "saghen/blink.cmp",
            dependencies= {
                "saghen/blink.lib",
            },
              build = function()
                require("blink.cmp").build():pwait()
              end,
    },
	},
}
