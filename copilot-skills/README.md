# copilot-skills

Personal (global) GitHub Copilot agent skills. Part of the `/opt/stacks` mono
repo — this folder is the source of truth and is symlinked into `~/.copilot/`
so VS Code Copilot, Copilot CLI, and the Copilot cloud agent all read from here.

## Why a symlink

VS Code Settings Sync does **not** cover `~/.copilot/`. Tracking the skills in
this mono repo and symlinking `~/.copilot/skills` at it means skills roam with
the rest of the setup (git clone + one symlink) instead.

## Setup (new machine)

```bash
# after cloning /opt/stacks
mkdir -p ~/.copilot
ln -s /opt/stacks/copilot-skills/skills ~/.copilot/skills
```

## Layout

```
copilot-skills/
├── README.md                    ← this file
└── skills/
    └── orchestrate/SKILL.md     ← orchestrator mode (see below)
```

## Skills

| Skill | Purpose |
|---|---|
| `orchestrate/` | Orchestrator mode: delegate implementation to full-toolset subagents (Qwen 3.8 27B LiteLLM), verify diffs, integrate |

## Notes

- Skills follow the open [Agent Skills standard](https://agentskills.io/) —
  `SKILL.md` frontmatter must have `name` matching its folder name.
- VS Code Settings Sync still covers user-profile prompts/instructions/agents
  (`~/.config/Code/User/…`); only skills need this repo treatment.
- Workspace-specific skills belong in each stack's own repo under
  `.github/skills/` (e.g. `/opt/stacks/trainlocks`), not here.