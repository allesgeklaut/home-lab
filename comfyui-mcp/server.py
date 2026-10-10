#!/usr/bin/env python3
"""ComfyUI MCP server for opencode.

Runs the Qwen-Image-2.1 workflows committed in `webui/workflows/` against the
ComfyUI lifecycle proxy (in the litellm stack), so opencode can generate and
edit images.

Why the proxy and not ComfyUI directly: the proxy starts the `comfyui`
container on demand and stops it when idle. Every mutating request here
(creating a prompt, uploading an input image) wakes it automatically, so this
server needs no lifecycle logic of its own.

Transport: stdio (launched by the MCP client, one process per session).

Tools:
  image_status    - proxy/ComfyUI state plus the available model files
  generate_image  - text to image (Turbo; Prompt-Enhancer rewrite opt-in)
  edit_image      - instruction edit of a local image file (Turbo; PE opt-in)

Config (env):
  COMFYUI_URL            base URL (default http://127.0.0.1:8189, the proxy)
  COMFYUI_WORKFLOWS_DIR  workflow JSON directory (default /opt/stacks/webui/workflows)
  COMFYUI_OUTPUT_DIR     host output dir, for reporting paths (default /opt/stacks/comfyui/output)
  COMFYUI_POLL_TIMEOUT   seconds to wait for a job (default 900)
"""

import json
import mimetypes
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from mcp.server.mcpserver import Image, MCPServer

BASE = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8189").rstrip("/")
WORKFLOWS_DIR = os.environ.get("COMFYUI_WORKFLOWS_DIR", "/opt/stacks/webui/workflows")
OUTPUT_DIR = os.environ.get("COMFYUI_OUTPUT_DIR", "/opt/stacks/comfyui/output")
POLL_TIMEOUT = int(os.environ.get("COMFYUI_POLL_TIMEOUT", "900"))

# All four run the Turbo UNet (qwen_image_2.1_turbo_int8_convrot) on its official
# 8-step sigma schedule via SamplerCustom + ManualSigmas. The `_pe_` variants add
# the Prompt Enhancer (opt-in via enhance=True). The base-model workflows
# (qwen_image_2_1_{t2i,edit}_api.json and their _pe_ variants) are kept on disk
# for rollback but are no longer referenced here.
T2I_WORKFLOW = os.path.join(WORKFLOWS_DIR, "qwen_image_2_1_turbo_t2i_api.json")
EDIT_WORKFLOW = os.path.join(WORKFLOWS_DIR, "qwen_image_2_1_turbo_edit_api.json")
PE_T2I_WORKFLOW = os.path.join(WORKFLOWS_DIR, "qwen_image_2_1_turbo_pe_t2i_api.json")
PE_EDIT_WORKFLOW = os.path.join(WORKFLOWS_DIR, "qwen_image_2_1_turbo_pe_edit_api.json")

server = MCPServer(
    name="comfyui",
    version="1.0.0",
    instructions=(
        "Generate and edit images with the local ComfyUI (Qwen-Image-2.1-Turbo, "
        "official 8-step schedule). Use generate_image for text-to-image and "
        "edit_image to modify an existing local image. Both run on Turbo by "
        "default (~25-40 s per image once warm). Pass enhance=true to "
        "additionally run the official Qwen-Image-2.1 Prompt Enhancer first (a "
        "Qwen3.5-VL-9B model that expands a short prompt, or grounds an edit "
        "instruction on the input image) before generation; the enhancer costs "
        "an extra ~1-2 min, so it is opt-in. The ComfyUI container is started on "
        "demand, so the first call after an idle period takes ~1 minute (cold "
        f"start plus model load). Files land in {OUTPUT_DIR}."
    ),
)


# ---------------------------------------------------------------- HTTP helpers


def _request(url, data=None, headers=None, method=None, timeout=120):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"{method or 'GET'} {url} -> HTTP {e.code}: {body}") from e


def _get_json(path):
    _, body, _ = _request(BASE + path, timeout=30)
    return json.loads(body)


def _post_json(path, payload, timeout=120):
    data = json.dumps(payload).encode()
    _, body, _ = _request(
        BASE + path, data=data, headers={"Content-Type": "application/json"},
        method="POST", timeout=timeout,
    )
    return json.loads(body) if body else {}


def _multipart(fields, filename, content_type, file_bytes):
    boundary = uuid.uuid4().hex
    buf = bytearray()
    for name, value in fields.items():
        buf += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
    buf += (
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"image\"; filename=\"{filename}\"\r\n"
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode()
    buf += file_bytes + f"\r\n--{boundary}--\r\n".encode()
    return bytes(buf), f"multipart/form-data; boundary={boundary}"


# ---------------------------------------------------------------- workflow ops


def _load_workflow(path):
    with open(path) as f:
        return json.load(f)


def _node_by_class(workflow, class_type):
    for node_id, node in workflow.items():
        if node.get("class_type") == class_type:
            return node_id, node
    raise RuntimeError(f"workflow has no {class_type} node")


def _apply_sampler(workflow, seed, steps):
    """Seed the sampler node of a workflow.

    Turbo workflows sample through SamplerCustom (input is `noise_seed`; the
    step count is fixed by their ManualSigmas node, so `steps` is ignored). The
    base workflows use KSampler (seed + steps). Both stay supported so a
    rollback to the base workflows keeps working.
    """
    for node in workflow.values():
        if node.get("class_type") == "SamplerCustom":
            node["inputs"]["noise_seed"] = seed
            return
        if node.get("class_type") == "KSampler":
            node["inputs"]["seed"] = seed
            node["inputs"]["steps"] = steps
            return
    raise RuntimeError("workflow has no sampler node")


def _submit_and_wait(workflow):
    """Submit a workflow and wait for it to finish. Returns the history entry."""
    prompt_id = _post_json("/prompt", {"prompt": workflow, "client_id": str(uuid.uuid4())})[
        "prompt_id"
    ]
    deadline = time.monotonic() + POLL_TIMEOUT
    while time.monotonic() < deadline:
        history = _get_json(f"/history/{prompt_id}")
        if history:
            entry = history[prompt_id]
            status = entry.get("status", {})
            if status.get("status_str") == "error":
                messages = status.get("messages", [])
                detail = next(
                    (m[1].get("exception_message", "") for m in messages if m[0] == "execution_error"),
                    "unknown error",
                )
                raise RuntimeError(f"ComfyUI rejected the workflow: {detail[:300]}")
            return entry
        time.sleep(3)
    raise RuntimeError(f"timed out after {POLL_TIMEOUT}s waiting for ComfyUI")


def _collect_images(entry, save_node):
    output = entry.get("outputs", {}).get(save_node, {})
    return output.get("images", [])


def _preview_source(entry, workflow, source_node, source_index):
    """Text of the PreviewAny node wired to (source_node, source_index), if any."""
    outputs = entry.get("outputs", {})
    for node_id, node in workflow.items():
        if node.get("class_type") != "PreviewAny":
            continue
        src = node["inputs"].get("source")
        if isinstance(src, list) and len(src) == 2 and src[0] == source_node and src[1] == source_index:
            text = outputs.get(node_id, {}).get("text")
            if text:
                return text[0] if isinstance(text, list) else text
    return ""


def _image_result(images, note):
    """Build the tool result: local paths + the first image inline."""
    if not images:
        raise RuntimeError("job finished but produced no images")
    paths = []
    for img in images:
        parts = [OUTPUT_DIR]
        if img.get("subfolder"):
            parts.append(img["subfolder"])
        parts.append(img["filename"])
        paths.append(os.path.join(*parts))
    lines = "\n".join(f"- {p}" for p in paths)
    local = [p for p in paths if os.path.exists(p)]
    if local:
        first = local[0]
        fmt = os.path.splitext(first)[1].lstrip(".") or "png"
        with open(first, "rb") as f:
            return [f"{note}\n{lines}", Image(data=f.read(), format=fmt)]
    # Not on this host (shouldn't happen): fall back to fetching via /view.
    img = images[0]
    query = urllib.parse.urlencode(
        {"filename": img["filename"], "subfolder": img.get("subfolder", ""), "type": img.get("type", "output")}
    )
    _, body, _ = _request(f"{BASE}/view?{query}", timeout=60)
    return [f"{note}\n{lines}", Image(data=body, format="png")]


# ---------------------------------------------------------------- tools


@server.tool()
def image_status() -> str:
    """Report whether ComfyUI is running (via the lifecycle proxy) and list the
    usable model files. The proxy answers /object_info from cache without
    starting the GPU, so this is cheap and safe to call any time."""
    health = _get_json("/health")
    lines = [f"proxy: {BASE}", f"comfyui: {health.get('comfyui', 'unknown')}"]
    try:
        info = _get_json("/object_info")
        unet = info.get("UNETLoader", {}).get("input", {}).get("required", {}).get("unet_name", [[]])[0]
        lines.append("diffusion models: " + ", ".join(unet))
    except Exception as e:  # object_info is best-effort
        lines.append(f"(model list unavailable: {e})")
    return "\n".join(lines)


def _generate_once(prompt, negative_prompt, width, height, steps, gen_seed, enhance):
    """Build and run one text-to-image pass.

    Returns (entry, save_node, enhanced_prompt, parse_ok). `parse_ok` is "True"
    when the enhancer is off (nothing to check); otherwise the rewrite node's
    "True"/"False".
    """
    workflow = _load_workflow(PE_T2I_WORKFLOW if enhance else T2I_WORKFLOW)
    rewrite_id = None
    if enhance:
        rewrite_id, rewrite = _node_by_class(workflow, "QwenImage21_T2IPromptRewrite")
        rewrite["inputs"]["prompt"] = prompt
        rewrite["inputs"]["seed"] = gen_seed
    else:
        _, te = _node_by_class(workflow, "TextEncodeQwenImage21")
        te["inputs"]["prompt"] = prompt
        te["inputs"]["negative_prompt"] = negative_prompt

    _, latent = _node_by_class(workflow, "EmptyLatentImage")
    latent["inputs"]["width"] = width
    latent["inputs"]["height"] = height
    _, te = _node_by_class(workflow, "TextEncodeQwenImage21")
    if "resolution" in te["inputs"]:
        te["inputs"]["resolution"] = max(width, height)
    _apply_sampler(workflow, gen_seed, steps)
    save_node, _ = _node_by_class(workflow, "SaveImage")

    entry = _submit_and_wait(workflow)
    if not enhance:
        return entry, save_node, "", "True"
    return (
        entry,
        save_node,
        _preview_source(entry, workflow, rewrite_id, 0),
        _preview_source(entry, workflow, rewrite_id, 4),  # parse_ok
    )


@server.tool()
def generate_image(
    prompt: str,
    negative_prompt: str = "",
    width: int = 1024,
    height: int = 1024,
    steps: int = 40,
    seed: int | None = None,
    enhance: bool = False,
) -> list:
    """Generate an image from a text prompt with Qwen-Image-2.1-Turbo.

    Runs the Turbo UNet on its official 8-step sigma schedule (fixed; `steps`
    is accepted for compatibility but ignored). `enhance` (default False)
    optionally first rewrites the prompt with the official Qwen-Image-2.1
    Prompt Enhancer (Qwen3.5-VL-9B), expanding a short request into a detailed
    description before generation — this adds ~1-2 min. If the rewrite fails to
    parse (e.g. the model refused, or the output was truncated), the call
    automatically retries once with the raw prompt.

    Returns the saved file path(s) and the image itself. `seed` defaults to
    random and drives both the sampler and, when enabled, the rewrite. cfg is
    fixed at 1, where `negative_prompt` is ignored. The first call after an idle
    period cold-starts ComfyUI (~1 minute).
    """
    gen_seed = seed if seed is not None else random.randint(1, 2**31)
    entry, save_node, enhanced, parse_ok = _generate_once(
        prompt, negative_prompt, width, height, steps, gen_seed, enhance
    )
    if enhance and parse_ok == "False":
        # the enhancer refused or produced an unparseable rewrite: use the raw prompt
        entry, save_node, _, _ = _generate_once(
            prompt, negative_prompt, width, height, steps, gen_seed, False
        )
        note = (
            f"Generated {width}x{height} (Turbo, seed {gen_seed}; "
            "enhancer rewrite failed, used the raw prompt)."
        )
    else:
        note = f"Generated {width}x{height} (Turbo, 8-step schedule; seed {gen_seed}"
        note += ", prompt enhanced)." if enhance else ")."
        if enhance and enhanced:
            note += f"\nEnhanced prompt: {enhanced}"
    return _image_result(_collect_images(entry, save_node), note)


def _edit_once(uploaded, prompt, steps, gen_seed, enhance):
    """Build and run one edit pass.

    Returns (entry, save_node, enhanced_instr, parse_ok). `parse_ok` is "True"
    when the enhancer is off (nothing to check); otherwise the rewrite node's
    "True"/"False".
    """
    workflow = _load_workflow(PE_EDIT_WORKFLOW if enhance else EDIT_WORKFLOW)
    load_node, load = _node_by_class(workflow, "LoadImage")
    load["inputs"]["image"] = uploaded
    _, te = _node_by_class(workflow, "TextEncodeQwenImage21")
    rewrite_id = None
    if enhance:
        rewrite_id, rewrite = _node_by_class(workflow, "QwenImage21_EditPromptRewrite")
        rewrite["inputs"]["prompt"] = prompt
        rewrite["inputs"]["seed"] = gen_seed
        rewrite["inputs"]["image_1"] = [load_node, 0]
        te["inputs"]["images.image_1"] = [load_node, 0]
    else:
        te["inputs"]["prompt"] = prompt
    _apply_sampler(workflow, gen_seed, steps)
    save_node, _ = _node_by_class(workflow, "SaveImage")

    entry = _submit_and_wait(workflow)
    if not enhance:
        return entry, save_node, "", "True"
    return (
        entry,
        save_node,
        _preview_source(entry, workflow, rewrite_id, 0),
        _preview_source(entry, workflow, rewrite_id, 5),  # parse_ok
    )


@server.tool()
def edit_image(
    image_path: str,
    prompt: str,
    steps: int = 40,
    seed: int | None = None,
    enhance: bool = False,
) -> list:
    """Edit a local image file with a natural-language instruction.

    Runs the Turbo UNet on its official 8-step sigma schedule (fixed; `steps` is
    accepted for compatibility but ignored). `image_path` is a path on this host
    (read by the server and uploaded to ComfyUI). `enhance` (default False)
    optionally first rewrites the instruction with the official Qwen-Image-2.1
    PE-I2I model, which *sees* the input image and grounds the instruction on it
    — this adds ~2-4 min. If the rewrite fails to parse (e.g. the model refused),
    the call automatically retries once with the raw instruction.

    Returns the saved result path(s) and the image. The output keeps the
    workflow's configured size (1024x1024).
    """
    if not os.path.isfile(image_path):
        raise RuntimeError(f"no such file: {image_path}")
    filename = os.path.basename(image_path)
    with open(image_path, "rb") as f:
        raw = f.read()
    ctype = mimetypes.guess_type(filename)[0] or "image/png"
    body, content_type = _multipart({"type": "input"}, filename, ctype, raw)
    _, resp, _ = _request(
        BASE + "/api/upload/image", data=body, headers={"Content-Type": content_type},
        method="POST", timeout=300,
    )
    uploaded = json.loads(resp).get("name", filename)

    gen_seed = seed if seed is not None else random.randint(1, 2**31)
    entry, save_node, enhanced, parse_ok = _edit_once(uploaded, prompt, steps, gen_seed, enhance)
    if enhance and parse_ok == "False":
        # the enhancer refused or produced an unparseable rewrite: use the raw instruction
        entry, save_node, _, _ = _edit_once(uploaded, prompt, steps, gen_seed, False)
        note = (
            f"Edited {filename} (Turbo, seed {gen_seed}; "
            "enhancer rewrite failed, used the raw instruction)."
        )
    else:
        note = f"Edited {filename} (Turbo, 8-step schedule; seed {gen_seed}"
        note += ", instruction enhanced)." if enhance else ")."
        if enhance and enhanced:
            note += f"\nEnhanced instruction: {enhanced}"
    return _image_result(_collect_images(entry, save_node), note)


if __name__ == "__main__":
    server.run("stdio")
