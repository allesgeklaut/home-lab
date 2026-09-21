# ComfyUI MCP server

A small stdio MCP server so opencode can generate and edit images with the
local ComfyUI (Qwen-Image-2.1).

It talks to the **comfyui-proxy** in the litellm stack rather than to ComfyUI
directly, so the on-demand lifecycle is handled for it: every mutating request
wakes the `comfyui` container, and the proxy stops it again when idle. There is
no lifecycle logic here.

## Tools

| tool | what it does |
|---|---|
| `image_status` | proxy/ComfyUI state and the available diffusion models. Uses the proxy's cached `object_info`, so it never starts the GPU. |
| `generate_image(prompt, negative_prompt, width=1024, height=1024, steps=40, seed=None)` | text to image |
| `edit_image(image_path, prompt, steps=40, seed=None)` | instruction edit of a local image file |

Both image tools return the saved path(s) under the ComfyUI output directory
**and** the image itself as MCP image content, so the agent can see the result.

## Wiring

Registered in `/opt/stacks/opencode.json` under `mcp.comfyui`:

```json
"comfyui": {
  "type": "local",
  "command": ["uv", "run", "--with", "mcp>=2,<3", "python", "/opt/stacks/comfyui-mcp/server.py"],
  "timeout": 600000,
  "environment": { "COMFYUI_URL": "http://192.168.0.46:8189" }
}
```

The only dependency is the official `mcp` SDK, resolved on demand by `uv`
(nothing is installed system-wide). The `timeout` is generous because a cold
generation takes over a minute.

Environment:

| var | default |
|---|---|
| `COMFYUI_URL` | `http://192.168.0.46:8189` (the proxy) |
| `COMFYUI_WORKFLOWS_DIR` | `/opt/stacks/webui/workflows` |
| `COMFYUI_OUTPUT_DIR` | `/opt/stacks/comfyui/output` |
| `COMFYUI_POLL_TIMEOUT` | `600` seconds |

## Workflows

It runs the committed API workflows
`webui/workflows/qwen_image_2_1_{t2i,edit}_api.json`, finding nodes by
`class_type` rather than hardcoded ids — so edits to those files (steps,
sampler, model filenames) are picked up with no change here. Only `cfg` is left
alone at `1`, the model's official path, where the negative prompt is ignored.

## Timing

The first call after ComfyUI has idled out takes about a minute (container
start plus model load); later calls are ~50 s at 40 steps. Measured at
1024x1024:

- `generate_image`, warm, 40 steps: ~48 s
- `edit_image`: ~150 s (the reference images go through the text encoder too)

## Relation to the official server

Comfy-Org's `comfy-mcp` also works against this proxy, but it is built on
`comfy-cli` (a host Python tool plus a workspace concept), is AGPL, and its
lifecycle/model/template tools target a *local* ComfyUI install — misleading
next to a container whose lifecycle is owned by `comfyui-proxy`. This server is
deliberately narrow: the two workflows we actually use, plus status.

(Comfy-cli's liveness check — `GET /history` expecting 200 — is why the proxy
answers that one read route with `200 {}` while ComfyUI is cold, instead of
`503`. That keeps status reads from starting the GPU.)
