# Global agent rules

These rules apply to every opencode session. This file lives in the
`/opt/stacks` repo and is symlinked from `~/.config/opencode/AGENTS.md` —
same convention as `~/.config/opencode/opencode.json -> /opt/stacks/opencode.json`.
Edit it here, not in `~/.config/`.

Same convention for the Vibe CLI: `~/.vibe/config.toml -> /opt/stacks/vibe/config.toml`,
`~/.vibe/agents -> /opt/stacks/vibe/agents`, `~/.vibe/skills -> /opt/stacks/vibe/skills`,
and `~/.vibe/.env -> /opt/secrets/vibe.env`
(vibe.env holds the vibe-cli quota key; mistral.env is the litellm key — see `vibe/README.md`)
— edit those in `vibe/`, not in `~/.vibe/`. See `vibe/README.md` for the secrets convention.
`vibe/config.toml` is also marked skip-worktree in git (vibe rewrites it through
the symlink on every persisted setting); unset the bit to commit deliberate
config changes — see `vibe/README.md`.

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
