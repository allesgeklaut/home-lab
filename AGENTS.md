# Global agent rules

These rules apply to every opencode session. This file lives in the
`/opt/stacks` repo and is symlinked from `~/.config/opencode/AGENTS.md` —
same convention as `~/.config/opencode/opencode.json -> /opt/stacks/opencode.json`.
Edit it here, not in `~/.config/`.

# Playwright (browser testing) — applies to all projects
* Screenshots and saved files go to `/opt/stacks/.playwright-out/` (configured
  via `--output-dir` on the playwright MCP server in this repo's `opencode.json`).
  Read them from there directly — never `find /` for them.
* Console logs / page snapshots land in `.playwright-mcp/` inside the repo
  under test; both directories are gitignore candidates in test repos.
* The browser container mounts `/opt/stacks` **read-only** (writes go only to
  `.playwright-out/`); secrets live in `/opt/secrets` and are never mounted.