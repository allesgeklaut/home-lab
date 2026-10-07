-- ============================================================================
-- DAP (Debug Adapter Protocol) Configuration
-- Modern setup for Python debugging with debugpy
-- ============================================================================

return {
  "mfussenegger/nvim-dap",
  dependencies = {
    "rcarriga/nvim-dap-ui",
    "theHamsta/nvim-dap-virtual-text",
    "nvim-neotest/nvim-nio",
  },
  keys = {
    { "<F5>",      function() require("dap").continue() end,          desc = "Debug: Start/Continue" },
    { "<F10>",     function() require("dap").step_over() end,         desc = "Debug: Step Over" },
    { "<F11>",     function() require("dap").step_into() end,         desc = "Debug: Step Into" },
    { "<F12>",     function() require("dap").step_out() end,          desc = "Debug: Step Out" },
    { "<leader>b", function() require("dap").toggle_breakpoint() end, desc = "Debug: Toggle Breakpoint" },
    {
      "<leader>B",
      function()
        require("dap").set_breakpoint(vim.fn.input("Breakpoint condition: "))
      end,
      desc = "Debug: Set Conditional Breakpoint"
    },
    { "<leader>dr", function() require("dap").repl.open() end, desc = "Debug: Open REPL" },
    { "<leader>dl", function() require("dap").run_last() end,  desc = "Debug: Run Last" },
    { "<leader>dt", function() require("dap").terminate() end, desc = "Debug: Terminate" },
    { "<leader>du", function() require("dapui").toggle() end,  desc = "Debug: Toggle UI" },
    { "<leader>de", function() require("dapui").eval() end,    desc = "Debug: Eval" },
  },
  config = function()
    local dap = require("dap")
    local dapui = require("dapui")

    -- ========================================================================
    -- DAP UI Setup
    -- ========================================================================
    dapui.setup({
      icons = { expanded = "▾", collapsed = "▸", current_frame = "▸" },
      mappings = {
        expand = { "<CR>", "<2-LeftMouse>" },
        open = "o",
        remove = "d",
        edit = "e",
        repl = "r",
        toggle = "t",
      },
      layouts = {
        {
          elements = {
            { id = "scopes",      size = 0.25 },
            { id = "breakpoints", size = 0.25 },
            { id = "stacks",      size = 0.25 },
            { id = "watches",     size = 0.25 },
          },
          size = 40,
          position = "left",
        },
        {
          elements = {
            { id = "repl",    size = 0.5 },
            { id = "console", size = 0.5 },
          },
          size = 10,
          position = "bottom",
        },
      },
      floating = {
        max_height = nil,
        max_width = nil,
        border = "rounded",
        mappings = {
          close = { "q", "<Esc>" },
        },
      },
    })

    -- ========================================================================
    -- Virtual Text Setup
    -- ========================================================================
    require("nvim-dap-virtual-text").setup({
      enabled = true,
      enabled_commands = true,
      highlight_changed_variables = true,
      highlight_new_as_changed = false,
      show_stop_reason = true,
      commented = false,
      only_first_definition = true,
      all_references = false,
      filter_references_pattern = "<module",
      virt_text_pos = "eol",
      all_frames = false,
      virt_lines = false,
      virt_text_win_col = nil,
    })

    -- ========================================================================
    -- Python Adapter Configuration
    -- ========================================================================
    dap.adapters.python = function(cb, config)
      if config.request == "attach" then
        -- Attach to a running debugpy server
        local port = (config.connect or config).port
        local host = (config.connect or config).host or "127.0.0.1"
        cb({
          type = "server",
          port = assert(port, "`connect.port` is required for a python `attach` configuration"),
          host = host,
          options = {
            source_filetype = "python",
          },
        })
      else
        -- Launch with debugpy
        local python_path
        if os.getenv("VIRTUAL_ENV") then
          python_path = os.getenv("VIRTUAL_ENV") .. "/bin/python"
        else
          local cwd = vim.fn.getcwd()
          if vim.fn.executable(cwd .. "/venv/bin/python") == 1 then
            python_path = cwd .. "/venv/bin/python"
          elseif vim.fn.executable(cwd .. "/.venv/bin/python") == 1 then
            python_path = cwd .. "/.venv/bin/python"
          else
            python_path = "/usr/bin/python3"
          end
        end

        cb({
          type = "executable",
          command = python_path,
          args = { "-m", "debugpy.adapter" },
          options = {
            source_filetype = "python",
          },
        })
      end
    end

    -- ========================================================================
    -- Python Debug Configurations
    -- ========================================================================
    dap.configurations.python = {
      {
        -- Launch current file
        type = "python",
        request = "launch",
        name = "Launch file",
        program = "${file}",
        pythonPath = function()
          local cwd = vim.fn.getcwd()
          if vim.fn.executable(cwd .. "/venv/bin/python") == 1 then
            return cwd .. "/venv/bin/python"
          elseif vim.fn.executable(cwd .. "/.venv/bin/python") == 1 then
            return cwd .. "/.venv/bin/python"
          elseif os.getenv("VIRTUAL_ENV") then
            return os.getenv("VIRTUAL_ENV") .. "/bin/python"
          else
            return "/usr/bin/python3"
          end
        end,
        console = "integratedTerminal",
        justMyCode = false,
      },
      {
        -- Launch with arguments
        type = "python",
        request = "launch",
        name = "Launch file with arguments",
        program = "${file}",
        args = function()
          local args_string = vim.fn.input("Arguments: ")
          return vim.split(args_string, " +")
        end,
        pythonPath = function()
          local cwd = vim.fn.getcwd()
          if vim.fn.executable(cwd .. "/venv/bin/python") == 1 then
            return cwd .. "/venv/bin/python"
          elseif vim.fn.executable(cwd .. "/.venv/bin/python") == 1 then
            return cwd .. "/.venv/bin/python"
          elseif os.getenv("VIRTUAL_ENV") then
            return os.getenv("VIRTUAL_ENV") .. "/bin/python"
          else
            return "/usr/bin/python3"
          end
        end,
        console = "integratedTerminal",
        justMyCode = false,
      },
      {
        -- Attach to running debugpy
        type = "python",
        request = "attach",
        name = "Attach to debugpy",
        connect = function()
          local host = vim.fn.input("Host [127.0.0.1]: ")
          host = host ~= "" and host or "127.0.0.1"
          local port = tonumber(vim.fn.input("Port [5678]: "))
          port = port or 5678
          return { host = host, port = port }
        end,
      },
      {
        -- Django
        type = "python",
        request = "launch",
        name = "Django",
        program = "${workspaceFolder}/manage.py",
        args = { "runserver" },
        pythonPath = function()
          local cwd = vim.fn.getcwd()
          if vim.fn.executable(cwd .. "/venv/bin/python") == 1 then
            return cwd .. "/venv/bin/python"
          elseif vim.fn.executable(cwd .. "/.venv/bin/python") == 1 then
            return cwd .. "/.venv/bin/python"
          elseif os.getenv("VIRTUAL_ENV") then
            return os.getenv("VIRTUAL_ENV") .. "/bin/python"
          else
            return "/usr/bin/python3"
          end
        end,
        console = "integratedTerminal",
        justMyCode = false,
      },
      {
        -- FastAPI
        type = "python",
        request = "launch",
        name = "FastAPI",
        module = "uvicorn",
        args = { "main:app", "--reload" },
        pythonPath = function()
          local cwd = vim.fn.getcwd()
          if vim.fn.executable(cwd .. "/venv/bin/python") == 1 then
            return cwd .. "/venv/bin/python"
          elseif vim.fn.executable(cwd .. "/.venv/bin/python") == 1 then
            return cwd .. "/.venv/bin/python"
          elseif os.getenv("VIRTUAL_ENV") then
            return os.getenv("VIRTUAL_ENV") .. "/bin/python"
          else
            return "/usr/bin/python3"
          end
        end,
        console = "integratedTerminal",
        justMyCode = false,
      },
      {
        -- Pytest
        type = "python",
        request = "launch",
        name = "Pytest",
        module = "pytest",
        args = { "${file}", "-v" },
        pythonPath = function()
          local cwd = vim.fn.getcwd()
          if vim.fn.executable(cwd .. "/venv/bin/python") == 1 then
            return cwd .. "/venv/bin/python"
          elseif vim.fn.executable(cwd .. "/.venv/bin/python") == 1 then
            return cwd .. "/.venv/bin/python"
          elseif os.getenv("VIRTUAL_ENV") then
            return os.getenv("VIRTUAL_ENV") .. "/bin/python"
          else
            return "/usr/bin/python3"
          end
        end,
        console = "integratedTerminal",
        justMyCode = false,
      },
    }

    -- ========================================================================
    -- DAP Signs (Modern API - Neovim 0.11+)
    -- ========================================================================
    vim.api.nvim_set_hl(0, "DapBreakpoint", { fg = "#e51400" })
    vim.api.nvim_set_hl(0, "DapBreakpointCondition", { fg = "#f79617" })
    vim.api.nvim_set_hl(0, "DapLogPoint", { fg = "#61afef" })
    vim.api.nvim_set_hl(0, "DapStopped", { fg = "#98c379" })
    vim.api.nvim_set_hl(0, "DapStoppedLine", { bg = "#31353f" })

    vim.fn.sign_define("DapBreakpoint", {
      text = "",
      texthl = "DapBreakpoint",
      linehl = "",
      numhl = "DapBreakpoint",
    })
    vim.fn.sign_define("DapBreakpointCondition", {
      text = "",
      texthl = "DapBreakpointCondition",
      linehl = "",
      numhl = "DapBreakpointCondition",
    })
    vim.fn.sign_define("DapLogPoint", {
      text = "",
      texthl = "DapLogPoint",
      linehl = "",
      numhl = "DapLogPoint",
    })
    vim.fn.sign_define("DapStopped", {
      text = "",
      texthl = "DapStopped",
      linehl = "DapStoppedLine",
      numhl = "DapStopped",
    })
    vim.fn.sign_define("DapBreakpointRejected", {
      text = "",
      texthl = "DapBreakpoint",
      linehl = "",
      numhl = "DapBreakpoint",
    })

    -- ========================================================================
    -- Auto-open/close DAP UI
    -- ========================================================================
    dap.listeners.before.attach.dapui_config = function()
      dapui.open()
    end
    dap.listeners.before.launch.dapui_config = function()
      dapui.open()
    end
    dap.listeners.before.event_terminated.dapui_config = function()
      dapui.close()
    end
    dap.listeners.before.event_exited.dapui_config = function()
      dapui.close()
    end
  end,
}
