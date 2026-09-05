# Global agent rules

These rules apply to every opencode session. This file lives in the
`/opt/stacks` repo and is symlinked from `~/.config/opencode/AGENTS.md` —
same convention as `~/.config/opencode/opencode.json -> /opt/stacks/opencode.json`.
Edit it here, not in `~/.config/`.

## Playwright (browser testing) — applies to all projects

* Screenshots, page snapshots, console logs — every artifact goes to
  `/opt/stacks/.playwright-out/` (configured via `--output-dir` on the
  playwright MCP server in this repo's `opencode.json`). Read them from
  there directly — never `find /` for them. Add the folder to the repo's
  `.gitignore` when testing a repo that tracks it.
* The browser container mounts `/opt/stacks` **read-only**; the only
  writable path is `.playwright-out/`. Secrets live in `/opt/secrets`
  and are never mounted into it.
* File uploads (`browser_file_upload`) take host paths and are read
  server-side inside the container — only paths under the mounted roots
  work; the MCP additionally jails access to its allowed workspace roots.