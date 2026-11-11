# Global agent rules

These rules apply to every opencode session. This file lives in the
`/opt/stacks` repo and is symlinked from `~/.config/opencode/AGENTS.md` —
same convention as `~/.config/opencode/opencode.json -> /opt/stacks/opencode.json`.
Edit it here, not in `~/.config/`.

## Opt-in orchestration

Select the global `orchestrator` or `alloy` primary agent to use the tier-worker
orchestration workflow. `alloy` works directly when suitable, delegates
selectively to `tier-1` through `tier-4`, and routes its own session model via
the session-model plugin (`get_session_model` / `switch_model`). With other
primary agents (including `build`), work directly by default; delegate only when
the user explicitly requests it or an applicable skill requires it. The presence
of tier workers alone is not a request to orchestrate. Project orchestration
procedures describe what to do when orchestration is enabled, not a requirement
to enable it for every task.

## Git merges — applies to all projects

When merging a feature branch into `main`, create a merge commit
(`git merge --no-ff <branch>`) instead of a fast-forward, so the branch's
history is preserved as a distinct unit.

## Playwright (browser testing) — applies to all projects

* Screenshots, page snapshots, console logs — every artifact goes to
  `/opt/stacks/.playwright-out/` (configured via `--output-dir` on the
  playwright MCP server in this repo's `opencode.json`). Read them from
  there directly — never `find /` for them. Add the folder to the repo's
  `.gitignore` when testing a repo that tracks it.
* `filename` on `browser_take_screenshot` MUST be an absolute path under
  `/opt/stacks/.playwright-out/` (e.g.
  `/opt/stacks/.playwright-out/my-shot.png`). A bare filename resolves
  against the browser's CWD (the repo dir) and fails with `EROFS:
  read-only file system` — the container's `--output-dir` only applies to
  auto-named artifacts, not to explicit relative paths.
* The browser container mounts `/opt/stacks` **read-only**; the only
  writable path is `.playwright-out/`. Secrets live in `/opt/secrets`
  and are never mounted into it.
* File uploads (`browser_file_upload`) take host paths and are read
  server-side inside the container — only paths under the mounted roots
  work; the MCP additionally jails access to its allowed workspace roots.

## Hugging Face CLI — applies to all projects

* The command is **`hf`** (installed at `~/.local/bin/hf`), e.g.
  `hf download <repo> <file> --local-dir <dir>` or `hf auth login`.
* Do **not** use `huggingface-cli`: it is the legacy name and is often absent
  or wrong on current installs. Agents reach for it by mistake — always use
  `hf` instead.
