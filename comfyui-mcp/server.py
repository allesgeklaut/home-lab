#!/usr/bin/env python3
"""Shared ComfyUI MCP server for opencode and Open WebUI.

Runs the Qwen-Image-2.1 workflows committed in `webui/workflows/` against the
ComfyUI lifecycle proxy (in the litellm stack), so opencode can generate and
edit images, and the local LTX-2.5 workflow for video/GIF/I2V.

Why the proxy and not ComfyUI directly: the proxy starts the `comfyui`
container on demand and stops it when idle. Every mutating request here
(creating a prompt, uploading an input image) wakes it automatically, so this
server needs no lifecycle logic of its own.

Transport: stdio or shared Streamable HTTP (COMFYUI_MCP_TRANSPORT).

Tools:
  image_status    - proxy/ComfyUI state plus the available model files
  generate_image  - text to image (Turbo, direct prompt)
  edit_image      - instruction edit with optional reference images (Turbo)
  generate_video  - LTX-2.5 clip (draft 512 or staged 1024, up to 10 s)

Config (env):
  COMFYUI_URL              base URL (default http://127.0.0.1:8189, the proxy)
  COMFYUI_WORKFLOWS_DIR    image workflow JSON directory (default /opt/stacks/webui/workflows)
  COMFYUI_VIDEO_WORKFLOW   LTX video workflow (default ./workflows/ltx25_distilled_t2v_api.json)
  COMFYUI_OUTPUT_DIR       host output dir, for reporting paths (default /opt/stacks/comfyui/output)
  COMFYUI_MODELS_DIR       host models dir (default /opt/stacks/comfyui/models)
  COMFYUI_EMBEDDINGS_DIR   conditioning cache (default $COMFYUI_MODELS_DIR/embeddings)
  COMFYUI_INPUT_DIR        host input dir for latent handoff (default /opt/stacks/comfyui/input)
  COMFYUI_POLL_TIMEOUT     seconds to wait for a job (default 900)
"""

import glob
import hashlib
import io
import json
import mimetypes
import os
import random
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from mcp.server.mcpserver import Image, MCPServer

BASE = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8189").rstrip("/")
PUBLIC_URL = os.environ.get("COMFYUI_PUBLIC_URL", BASE).rstrip("/")
WORKFLOWS_DIR = os.environ.get("COMFYUI_WORKFLOWS_DIR", "/opt/stacks/webui/workflows")
OUTPUT_DIR = os.environ.get("COMFYUI_OUTPUT_DIR", "/opt/stacks/comfyui/output")
OUTPUT_HOST_DIR = os.environ.get("COMFYUI_OUTPUT_HOST_DIR", OUTPUT_DIR)
MODELS_DIR = os.environ.get("COMFYUI_MODELS_DIR", "/opt/stacks/comfyui/models")
EMBEDDINGS_DIR = os.environ.get(
    "COMFYUI_EMBEDDINGS_DIR", os.path.join(MODELS_DIR, "embeddings")
)
INPUT_DIR = os.environ.get("COMFYUI_INPUT_DIR", "/opt/stacks/comfyui/input")
INPUT_HOST_DIR = os.environ.get("COMFYUI_INPUT_HOST_DIR", "/opt/stacks/comfyui/input")
POLL_TIMEOUT = int(os.environ.get("COMFYUI_POLL_TIMEOUT", "900"))

# Subfolder under the ComfyUI output root for everything this server writes.
# Kept separate from the historical `video-experiments/` scratch folder.
VIDEO_PREFIX = "mcp-video"
# Leave room for base64 expansion and MCP metadata below a 1 MB SSE event.
INLINE_IMAGE_BYTES = 700_000

# Both run the Turbo UNet on its official 8-step sigma schedule. The base-model
# workflows stay on disk for rollback. Prompts are passed through unchanged.
T2I_WORKFLOW = os.path.join(WORKFLOWS_DIR, "qwen_image_2_1_turbo_t2i_api.json")
EDIT_WORKFLOW = os.path.join(WORKFLOWS_DIR, "qwen_image_2_1_turbo_edit_api.json")
VIDEO_WORKFLOW = os.environ.get(
    "COMFYUI_VIDEO_WORKFLOW",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "workflows",
                 "ltx25_distilled_t2v_api.json"),
)

# LTX-2.5 distilled 22B INT8. The text encoder and the video DiT cannot share
# 31 GB of host RAM, so generate_video encodes, recycles the container through
# the proxy, then generates. These names must match the files in comfyui/models.
LTX_ENCODER = "gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors"
LTX_VIDEO_VAE = "ltx-2.5-video-vae-conv-bf16.safetensors"
LTX_HQ_VIDEO_VAE = "ltx-2.5-video-vae-bf16.safetensors"
LTX_UPSCALER = "ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors"

_VIDEO_LOCK = threading.Lock()

server = MCPServer(
    name="comfyui",
    version="1.2.0",
    instructions=(
        "Generate and edit images with the local ComfyUI (Qwen-Image-2.1-Turbo, "
        "official 8-step schedule), and generate short LTX-2.5 video clips. Use "
        "generate_image for text-to-image, edit_image to modify an existing local "
        "image, and generate_video for video. Image generation defaults to "
        "2048x2048; edits preserve input alpha and aspect ratio at a 1536 "
        "resolution budget. Up to 9 extra reference_images can accompany an "
        "edit. Video defaults to a 512 draft; quality='final' runs the staged "
        "1024 pipeline. The ComfyUI container is started on demand, so the "
        "first call after an idle period takes ~1 minute (cold start plus model "
        f"load). Files land in {OUTPUT_HOST_DIR}."
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


def _mem_available_mb():
    """Host MemAvailable in MiB, or None when /proc/meminfo is unavailable."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return None


def _cancel_job(prompt_id):
    """Best-effort cleanup after a failed job.

    Interrupt our running job and remove only its queue entry. If still wedged in
    ComfyUI's queue, ask the proxy to force a clean stop for that prompt only;
    the proxy still refuses if another client's job is queued.
    """
    try:
        queue = _get_json("/queue")
        running = [item[1] for item in queue.get("queue_running", [])]
        if running == [prompt_id]:
            _post_json("/interrupt", {}, timeout=30)
        # Delete only this job; never clear another client's pending work.
        _post_json("/queue", {"delete": [prompt_id]}, timeout=30)
    except Exception:
        pass
    try:
        _recycle(prompt_id=prompt_id)
    except Exception:
        pass


def _submit_and_wait(workflow, guard_memory=False):
    """Submit a workflow and wait for it to finish. Returns the history entry.

    With guard_memory, interrupt our own job if the host drops below 1 GiB
    available. Video phases load 15-22 GB models, and an OOM kill would take
    the whole ComfyUI process (and the proxy's lifecycle state) with it.
    """
    prompt_id = _post_json("/prompt", {"prompt": workflow, "client_id": str(uuid.uuid4())})[
        "prompt_id"
    ]
    deadline = time.monotonic() + POLL_TIMEOUT
    try:
        while time.monotonic() < deadline:
            if guard_memory:
                available = _mem_available_mb()
                if available is not None and available < 1024:
                    try:
                        queue = _get_json("/queue")
                    except Exception:
                        queue = {}
                    running = [item[1] for item in queue.get("queue_running", [])]
                    if running == [prompt_id] and not queue.get("queue_pending"):
                        raise RuntimeError(
                            f"memory safety stop: only {available} MB available"
                        )
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
    except Exception:
        _cancel_job(prompt_id)
        raise


def _collect_images(entry, save_node):
    output = entry.get("outputs", {}).get(save_node, {})
    return output.get("images", [])


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
    lines = "\n".join(f"- {_host_path(img)}" for img in images)
    local = [p for p in paths if os.path.exists(p)]
    if local:
        first = local[0]
        return [f"{note}\n{lines}", _inline_still(first)]
    # Not on this host: fetch via /view and SAVE a local copy under
    # OUTPUT_DIR, so agents get real files on this machine (ComfyUI itself
    # runs remotely); the first image is also returned inline as before.
    for img, dest in zip(images, paths):
        query = urllib.parse.urlencode(
            {"filename": img["filename"], "subfolder": img.get("subfolder", ""), "type": img.get("type", "output")}
        )
        _, body, _ = _request(f"{BASE}/view?{query}", timeout=60)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as fh:
            fh.write(body)
    return [f"{note}\n{lines}", _inline_still(paths[0])]


def _inline_still(path):
    """Bound inline previews for HTTP MCP; leave the full output file untouched.

    Use PNG when shrinking so transparent image outputs retain their alpha.
    """
    with open(path, "rb") as source:
        raw = source.read()
    fmt = os.path.splitext(path)[1].lstrip(".").lower() or "png"
    if len(raw) <= INLINE_IMAGE_BYTES:
        return Image(data=raw, format="jpeg" if fmt == "jpg" else fmt)
    from PIL import Image as PILImage

    with PILImage.open(io.BytesIO(raw)) as source:
        preview = source.convert("RGBA")
    edge = min(1024, max(preview.size))
    while True:
        preview.thumbnail((edge, edge), PILImage.Resampling.LANCZOS)
        buffer = io.BytesIO()
        preview.save(buffer, format="PNG")
        raw = buffer.getvalue()
        if len(raw) <= INLINE_IMAGE_BYTES:
            return Image(data=raw, format="png")
        edge = max(1, edge * 3 // 4)


# ---------------------------------------------------------------- video ops


def _video_frames(seconds):
    """Nearest valid LTX length (8n+1 frames) to *seconds* at 24 fps."""
    frames = max(9, round((seconds * 24 - 1) / 8) * 8 + 1)
    return min(frames, 241)


# Pixel budget for clips over 6 s. The 22B DiT already fills the 16 GB card at
# 49 frames, so long clips offload more layers instead of fitting more
# activations. Measured on this host, 10 s final at 1024x1024 (31,744 tokens)
# still finishes in 484 s with 3.8 GiB of host RAM free, so final has no pixel
# cap. Draft keeps 512x512: a larger long draft costs as much as a 16:9 final
# but produces a worse frame, so above 6 s the fast path stays fast.
_LONG_CLIP_PIXELS = {"draft": 512 * 512}


def _check_long_clip(seconds, quality, width, height):
    if seconds <= 6.0:
        return
    budget = _LONG_CLIP_PIXELS.get(quality)
    if budget is None:
        return
    if width * height > budget:
        edge = int(budget ** 0.5)
        raise RuntimeError(
            f"clips over 6 s are capped at {edge}x{edge} pixels for "
            f"quality='{quality}'; use quality='final' for a larger frame at "
            "the same length"
        )


# Largest validated render is ~1 MP. The official template's 1280x720
# (1280x704 after VAE alignment) and 1024x1024 both fit; 1280x1280 does not.
_MAX_PIXELS = 1024 * 1024


def _video_dimensions(quality, size, aspect_ratio):
    """Longest-edge presets, aligned to the VAE (and staged half-size pass)."""
    if size is None:
        size = 512 if quality == "draft" else 1024
    if size % 64 or not 256 <= size <= 1280:
        raise RuntimeError("size must be 256..1280 and divisible by 64")
    if aspect_ratio not in {"1:1", "16:9", "9:16"}:
        raise RuntimeError("aspect_ratio must be '1:1', '16:9', or '9:16'")
    if aspect_ratio == "1:1":
        width = height = size
    else:
        alignment = 64 if quality == "final" else 32
        short = max(128, round(size * 9 / 16 / alignment) * alignment)
        width, height = (size, short) if aspect_ratio == "16:9" else (short, size)
    if width * height > _MAX_PIXELS:
        raise RuntimeError(
            f"{width}x{height} exceeds the ~1 MP budget; use size=1280 with "
            "aspect_ratio='16:9' (1280x704) or size=1024 for square"
        )
    return width, height


def _conditioning_name(prompt):
    digest = hashlib.sha256(prompt.encode()).hexdigest()[:16]
    return f"ltx25_{digest}.safetensors"


def _conditioning_cached(name):
    target = os.path.join(EMBEDDINGS_DIR, name)
    return os.path.isfile(target) and os.path.getsize(target) > 0


def _ensure_conditioning(prompt, name):
    """Encode the prompt once and cache the conditioning in models/embeddings.

    Returns True when the encoder ran, False on a cache hit. The copy is what
    makes the next process see it: ConditioningLoader only lists the embeddings
    folder, and SaveConditioning writes to the output folder.
    """
    target = os.path.join(EMBEDDINGS_DIR, name)
    if _conditioning_cached(name):
        return False
    prefix = f"{VIDEO_PREFIX}/conditioning/mcp_{uuid.uuid4().hex}"
    graph = {
        "1": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": LTX_ENCODER, "type": "ltxv", "device": "default"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["1", 0], "text": prompt}},
        "3": {"class_type": "SaveConditioning", "inputs": {
            "conditioning": ["2", 0], "filename_prefix": prefix}},
    }
    _submit_and_wait(graph, guard_memory=True)
    matches = sorted(
        glob.glob(os.path.join(OUTPUT_DIR, prefix + "_*.safetensors")),
        key=os.path.getmtime,
    )
    if not matches:
        raise RuntimeError("text encoder finished but produced no conditioning file")
    os.makedirs(EMBEDDINGS_DIR, exist_ok=True)
    shutil.copy2(matches[-1], target)
    # The embeddings copy is the cache ConditioningLoader reads; the
    # output-side intermediate is redundant once copied. Keep it only when
    # the copy failed (the exception propagates before this loop runs).
    for path in matches:
        try:
            os.remove(path)
        except OSError:
            pass
    return True


def _recycle(prompt_id=None):
    """Ask the lifecycle proxy for a clean ComfyUI process between passes.

    /free unloads VRAM but leaves host RAM in the process, and the 21.5 GB DiT
    cannot load after the 12.8 GB text encoder. The proxy owns start/stop; this
    server never talks to Docker or Portainer.

    With prompt_id, the proxy may stop a queue that contains only that prompt;
    this is the recovery path for a job that wedged during model loading.
    """
    payload = {"prompt_id": prompt_id} if prompt_id else {}
    try:
        result = _post_json("/recycle", payload, timeout=180)
    except RuntimeError as exc:
        raise RuntimeError(
            "cannot recycle ComfyUI between passes (another job may be running): "
            f"{exc}"
        ) from exc
    if result.get("comfyui") != "down":
        raise RuntimeError(f"recycle did not stop ComfyUI: {result}")


def _video_graph(prompt, width, height, frames, gen_seed, conditioning_name, staged,
                 image_name=None, strength=1.0):
    graph = _load_workflow(VIDEO_WORKFLOW)
    graph["3"]["inputs"]["text"] = prompt
    graph["4"]["inputs"]["text"] = ""
    graph["5"]["inputs"].update(width=width, height=height, length=frames, batch_size=1)
    graph["6"]["inputs"]["frame_rate"] = 24.0
    graph["14"]["inputs"]["noise_seed"] = gen_seed
    graph["17"]["inputs"].update(frames_number=frames, frame_rate=24.0)
    del graph["2"]
    graph["3"] = {"class_type": "ConditioningLoader", "inputs": {
        "conditioning_name": conditioning_name}}
    graph["4"] = {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["3", 0]}}
    if staged:
        # Official two-stage recipe: half-size base, 2x latent upscale, then
        # three refinement steps at the requested size with the HQ VAE.
        graph["5"]["inputs"].update(width=width // 2, height=height // 2)
        graph["15"]["inputs"]["vae_name"] = LTX_HQ_VIDEO_VAE
        graph.update({
            "21": {"class_type": "LatentUpscaleModelLoader", "inputs": {
                "model_name": LTX_UPSCALER}},
            "22": {"class_type": "LTXVLatentUpsampler", "inputs": {
                "samples": ["19", 0], "upscale_model": ["21", 0], "vae": ["15", 0]}},
            "23": {"class_type": "LTXVConcatAVLatent", "inputs": {
                "video_latent": ["22", 0], "audio_latent": ["19", 1]}},
            "24": {"class_type": "ManualSigmas", "inputs": {
                "sigmas": "0.85, 0.7250, 0.4219, 0.0"}},
            "25": {"class_type": "RandomNoise", "inputs": {"noise_seed": 42}},
            "26": {"class_type": "SamplerCustomAdvanced", "inputs": {
                "noise": ["25", 0], "guider": ["13", 0], "sampler": ["8", 0],
                "sigmas": ["24", 0], "latent_image": ["23", 0]}},
            "27": {"class_type": "LTXVSeparateAVLatent", "inputs": {"av_latent": ["26", 0]}},
        })
        graph["10"]["inputs"]["samples"] = ["27", 0]
        graph["20"]["inputs"]["samples"] = ["27", 1]
    elif not image_name:
        # Text-to-video drafts decode with the HQ VAE, matching the official
        # template. Measured on this host: 25-45% less global luminance
        # flicker and 9-17% lower worst-frame spikes than the conv VAE, with
        # identical motion. I2V keeps the conv VAE because node 15 also
        # encodes the first frame there.
        graph["15"]["inputs"]["vae_name"] = LTX_HQ_VIDEO_VAE
    if image_name:
        # First-frame image-to-video. The image is encoded by the same VAE the
        # video decoder uses; the staged base phase needs its own conv VAE
        # because node 15 is the HQ decoder there.
        base_width, base_height = (width // 2, height // 2) if staged else (width, height)
        vae_node = "53" if staged else "15"
        if staged:
            graph["53"] = {"class_type": "VAELoader", "inputs": {"vae_name": LTX_VIDEO_VAE}}
        graph["50"] = {"class_type": "LoadImage", "inputs": {"image": image_name}}
        graph["51"] = {"class_type": "LTXVPreprocess", "inputs": {
            "image": ["50", 0], "img_compression": 35}}
        graph["52"] = {"class_type": "LTXVImgToVideo", "inputs": {
            "positive": ["6", 0], "negative": ["6", 1], "vae": [vae_node, 0],
            "image": ["51", 0], "width": base_width, "height": base_height,
            "length": frames, "batch_size": 1, "strength": strength}}
        graph["13"]["inputs"]["positive"] = ["52", 0]
        graph["13"]["inputs"]["negative"] = ["52", 1]
        graph["18"]["inputs"]["video_latent"] = ["52", 2]
        del graph["5"]
    return graph


def _ancestors(graph, outputs):
    """Prune a graph to the nodes needed for *outputs* (one isolated phase)."""
    selected = {}

    def visit(node_id):
        if node_id in selected:
            return
        node = graph[node_id]
        selected[node_id] = node
        for value in node["inputs"].values():
            if isinstance(value, list) and len(value) == 2:
                visit(value[0])

    for output in outputs:
        visit(output)
    return selected


def _save_latent(link, prefix):
    return {"class_type": "SaveLatent", "inputs": {"samples": link, "filename_prefix": prefix}}


def _load_latent(name):
    return {"class_type": "LoadLatent", "inputs": {"latent": name}}


def _copy_latent(entry, node_id, run_id, label):
    items = entry.get("outputs", {}).get(node_id, {}).get("latents", [])
    if len(items) != 1:
        raise RuntimeError(f"expected one {label} latent, got {len(items)}")
    item = items[0]
    source = os.path.join(OUTPUT_DIR, item.get("subfolder", ""), item["filename"])
    filename = f"{run_id}_{label}.latent"
    os.makedirs(INPUT_DIR, exist_ok=True)
    shutil.copy2(source, os.path.join(INPUT_DIR, filename))
    return filename


def _add_poster(graph, prefix):
    graph["40"] = {"class_type": "ImageFromBatch", "inputs": {
        "image": ["10", 0], "batch_index": 0, "length": 1}}
    graph["41"] = {"class_type": "SaveImage", "inputs": {
        "images": ["40", 0], "filename_prefix": prefix}}


def _host_path(item):
    return os.path.join(OUTPUT_HOST_DIR, item.get("subfolder", ""), item["filename"])


def _media_url(item):
    query = urllib.parse.urlencode({
        "filename": item["filename"],
        "subfolder": item.get("subfolder", ""),
        "type": item.get("type", "output"),
    })
    return f"{PUBLIC_URL}/view?{query}"


def _video_result(entry, note):
    videos = _collect_images(entry, "12")
    if not videos:
        raise RuntimeError("job finished but produced no video")
    video = next(
        (item for item in videos
         if str(item.get("filename", "")).lower().endswith((".mp4", ".webm", ".mkv"))),
        videos[0],
    )
    poster = None
    posters = _collect_images(entry, "41")
    if posters:
        poster = posters[0]
    return {
        "video": os.path.join(OUTPUT_DIR, video.get("subfolder", ""), video["filename"]),
        "video_host": _host_path(video),
        "video_url": _media_url(video),
        "poster": os.path.join(OUTPUT_DIR, poster.get("subfolder", ""), poster["filename"]) if poster else None,
        "poster_host": _host_path(poster) if poster else None,
        "note": note,
    }


def _format_video_result(result):
    lines = [result["note"], f"- video: {result['video_host']}"]
    if result.get("video_url"):
        lines.append(f"- url: {result['video_url']}")
    poster = result.get("poster")
    if poster and os.path.isfile(poster):
        lines.append(f"- poster: {result['poster_host']}")
        return ["\n".join(lines), _inline_still(poster)]
    return "\n".join(lines)


def _mp4_to_gif(source, dest, max_width=360, target_fps=8, colors=128):
    """Convert an MP4 to a looping GIF with PyAV + Pillow.

    Frames are decimated to *target_fps* (meme GIFs are 8-15 fps, not 24),
    optionally quantized to *colors*, and given centisecond delays so the
    duration does not drift. The defaults keep the base64 payload under the
    1 MB SSE event limit used by MCP clients.
    """
    import av
    from PIL import Image as PILImage

    container = av.open(source)
    stream = container.streams.video[0]
    source_fps = float(stream.average_rate) if stream.average_rate else 24.0
    frames = []
    for frame in container.decode(stream):
        img = frame.to_image().convert("RGB")
        if img.width > max_width:
            height = round(img.height * max_width / img.width)
            img = img.resize((max_width, height), PILImage.LANCZOS)
        frames.append(img)
    container.close()
    if not frames:
        raise RuntimeError("no frames decoded for GIF")

    step = max(1, round(source_fps / target_fps))
    if step > 1:
        frames = frames[::step]
        source_fps = source_fps / step
    if colors < 256:
        frames = [
            frame.quantize(colors=colors, method=PILImage.MEDIANCUT)
            for frame in frames
        ]
    durations = [
        max(10, (round((i + 1) * 100 / source_fps) - round(i * 100 / source_fps)) * 10)
        for i in range(len(frames))
    ]
    frames[0].save(
        dest, save_all=True, append_images=frames[1:],
        duration=durations, loop=0, optimize=True,
    )
    return len(frames), sum(durations)


def _cleanup_latents(run_id):
    """Remove a successful run's intermediate latents; keep them on failure."""
    for directory in (INPUT_DIR, os.path.join(OUTPUT_DIR, VIDEO_PREFIX, "staged")):
        for path in glob.glob(os.path.join(directory, f"{run_id}_*.latent")):
            try:
                os.remove(path)
            except OSError:
                pass


def _generate_video(prompt, staged, width, height, frames, gen_seed, label,
                    image_path=None, strength=1.0):
    run_id = f"ltx25_mcp_{uuid.uuid4().hex[:10]}"
    stem = f"{run_id}_{width}x{height}_{frames}f"
    conditioning = _conditioning_name(prompt)
    if not _conditioning_cached(conditioning):
        # A previous video process still holds the DiT in host RAM. Stop it
        # before loading the 12.8 GB text encoder; /free alone does not return
        # that memory.
        _recycle()
    encoded = _ensure_conditioning(prompt, conditioning)
    note = (
        f"LTX-2.5 {label} {width}x{height}, {frames} frames at 24 fps "
        f"({frames / 24:.1f} s); seed {gen_seed}; "
        f"{'prompt encoded' if encoded else 'prompt conditioning cached'}"
    )
    if image_path:
        note += f"; first frame {os.path.basename(image_path)} (strength {strength:g})"
    note += "."
    _recycle()

    image_name = _upload_image_file(image_path) if image_path else None

    if not staged:
        graph = _video_graph(
            prompt, width, height, frames, gen_seed, conditioning, staged=False,
            image_name=image_name, strength=strength,
        )
        graph["12"]["inputs"]["filename_prefix"] = f"{VIDEO_PREFIX}/{stem}"
        _add_poster(graph, f"{VIDEO_PREFIX}/{stem}_poster")
        entry = _submit_and_wait(graph, guard_memory=True)
        return _video_result(entry, note)

    graph = _video_graph(
        prompt, width, height, frames, gen_seed, conditioning, staged=True,
        image_name=image_name, strength=strength,
    )
    prefix = f"{VIDEO_PREFIX}/staged/{run_id}"
    graph["30"] = _save_latent(["19", 0], prefix + "_base_video")
    graph["31"] = _save_latent(["19", 1], prefix + "_base_audio")
    entry = _submit_and_wait(_ancestors(graph, ["30", "31"]), guard_memory=True)
    video = _copy_latent(entry, "30", run_id, "base_video")
    audio = _copy_latent(entry, "31", run_id, "base_audio")
    _recycle()

    graph["28"] = _load_latent(video)
    graph["22"]["inputs"]["samples"] = ["28", 0]
    graph["30"] = _save_latent(["22", 0], prefix + "_upscaled_video")
    entry = _submit_and_wait(_ancestors(graph, ["30"]), guard_memory=True)
    upscaled = _copy_latent(entry, "30", run_id, "upscaled_video")
    _recycle()

    graph["22"] = _load_latent(upscaled)
    graph["28"] = _load_latent(audio)
    graph["23"]["inputs"]["audio_latent"] = ["28", 0]
    graph["30"] = _save_latent(["27", 0], prefix + "_refined_video")
    graph["31"] = _save_latent(["27", 1], prefix + "_refined_audio")
    entry = _submit_and_wait(_ancestors(graph, ["30", "31"]), guard_memory=True)
    refined_video = _copy_latent(entry, "30", run_id, "refined_video")
    refined_audio = _copy_latent(entry, "31", run_id, "refined_audio")
    _recycle()

    graph["28"] = _load_latent(refined_video)
    graph["29"] = _load_latent(refined_audio)
    graph["10"]["inputs"]["samples"] = ["28", 0]
    graph["20"]["inputs"]["samples"] = ["29", 0]
    graph["12"]["inputs"]["filename_prefix"] = f"{VIDEO_PREFIX}/{stem}"
    _add_poster(graph, f"{VIDEO_PREFIX}/{stem}_poster")
    entry = _submit_and_wait(_ancestors(graph, ["12", "41"]), guard_memory=True)
    result = _video_result(entry, note)
    _cleanup_latents(run_id)
    return result


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


def _generate_once(prompt, negative_prompt, width, height, steps, gen_seed):
    """Build and run one text-to-image pass; return (entry, save_node)."""
    workflow = _load_workflow(T2I_WORKFLOW)
    _, te = _node_by_class(workflow, "TextEncodeQwenImage21")
    te["inputs"]["prompt"] = prompt
    te["inputs"]["negative_prompt"] = negative_prompt

    _, latent = _node_by_class(workflow, "EmptyLatentImage")
    latent["inputs"]["width"] = width
    latent["inputs"]["height"] = height
    _, te = _node_by_class(workflow, "TextEncodeQwenImage21")
    if "resolution" in te["inputs"]:
        # official semantics: total pixel budget (~sqrt(w*h)), rounded to 32 —
        # identical to max(w,h) only for squares, correct for every aspect
        te["inputs"]["resolution"] = round((width * height) ** 0.5 / 32) * 32
    _apply_sampler(workflow, gen_seed, steps)
    save_node, _ = _node_by_class(workflow, "SaveImage")

    return _submit_and_wait(workflow), save_node


@server.tool()
def generate_image(
    prompt: str,
    negative_prompt: str = "",
    width: int = 2048,
    height: int = 2048,
    steps: int = 40,
    seed: int | None = None,
) -> list:
    """Generate an image from a text prompt with Qwen-Image-2.1-Turbo.

    Runs the Turbo UNet on its official 8-step sigma schedule (fixed; `steps`
    is accepted for compatibility but ignored). The prompt is passed through
    unchanged, so describe the subject, composition, and background explicitly.

    Returns the saved file path(s) and the image itself. `seed` defaults to
    random and drives the sampler. cfg is
    fixed at 1, where `negative_prompt` is ignored. The first call after an idle
    period cold-starts ComfyUI (~1 minute).
    """
    gen_seed = seed if seed is not None else random.randint(1, 2**31)
    if not _VIDEO_LOCK.acquire(blocking=False):
        raise RuntimeError("another generation is already running; wait for it to finish")
    try:
        entry, save_node = _generate_once(
            prompt, negative_prompt, width, height, steps, gen_seed
        )
        note = f"Generated {width}x{height} (Turbo, 8-step schedule; seed {gen_seed})."
        return _image_result(_collect_images(entry, save_node), note)
    finally:
        _VIDEO_LOCK.release()


def _load_image_nodes(workflow):
    """LoadImage node ids in numeric order: the first is the edit target, the
    rest are the optional reference slots (image_2..image_10; unfilled slots
    keep the sentinel filename, which the lifecycle proxy strips)."""
    return sorted(
        (nid for nid, n in workflow.items() if n.get("class_type") == "LoadImage"),
        key=int,
    )


def _upload_image_file(image_path):
    """Upload a local image to ComfyUI and return the name it was stored as.

    A file already in the shared input directory is visible to ComfyUI as-is,
    so it is used by basename without an upload round trip.
    """
    resolved = os.path.realpath(image_path)
    input_root = os.path.realpath(INPUT_DIR)
    if os.path.dirname(resolved) == input_root:
        return os.path.basename(resolved)
    filename = os.path.basename(image_path)
    with open(image_path, "rb") as f:
        raw = f.read()
    ctype = mimetypes.guess_type(filename)[0] or "image/png"
    body, content_type = _multipart({"type": "input"}, filename, ctype, raw)
    _, resp, _ = _request(
        BASE + "/api/upload/image", data=body, headers={"Content-Type": content_type},
        method="POST", timeout=300,
    )
    return json.loads(resp).get("name", filename)


def _local_image_path(path):
    """Translate advertised host paths into the narrow container mounts."""
    candidate = os.path.abspath(path)
    for host_root, local_root in ((OUTPUT_HOST_DIR, OUTPUT_DIR),
                                  (INPUT_HOST_DIR, INPUT_DIR)):
        root = os.path.abspath(host_root)
        if os.path.commonpath((candidate, root)) == root:
            return os.path.join(local_root, os.path.relpath(candidate, root))
    return path


def _edit_once(uploads, prompt, steps, gen_seed):
    """Build and run one edit pass.

    `uploads` is the list of ComfyUI image names: uploads[0] is the edit
    target; further entries fill the reference slots in order (remaining
    slots keep the sentinel and are stripped by the lifecycle proxy).

    Returns (entry, save_node).
    """
    workflow = _load_workflow(EDIT_WORKFLOW)
    load_nodes = _load_image_nodes(workflow)
    if len(uploads) > len(load_nodes):
        raise RuntimeError(
            f"edit workflow has {len(load_nodes)} image slots, but "
            f"{len(uploads)} images were supplied; update the workflow templates"
        )
    for nid, name in zip(load_nodes, uploads):
        workflow[nid]["inputs"]["image"] = name
    _, te = _node_by_class(workflow, "TextEncodeQwenImage21")
    te["inputs"]["prompt"] = prompt
    _apply_sampler(workflow, gen_seed, steps)
    save_node, _ = _node_by_class(workflow, "SaveImage")

    return _submit_and_wait(workflow), save_node


@server.tool()
def edit_image(
    image_path: str,
    prompt: str,
    steps: int = 40,
    seed: int | None = None,
    reference_images: list[str] | None = None,
) -> list:
    """Edit a local image file with a natural-language instruction, optionally
    with extra reference images.

    Runs the Turbo UNet on its official 8-step sigma schedule (fixed; `steps` is
    accepted for compatibility but ignored). `image_path` is the edit target:
    a path on this host (read by the server and uploaded to ComfyUI).
    `reference_images` optionally supplies up to 9 further host paths used as
    references — Qwen-Image-2.1 editing is reference-based generation, not
    just retouching, so e.g. character portraits can be composed into a new
    scene. Address them in the prompt as `<image2>` .. `<image10>` (the target
    is `<image1>`); unused slots are stripped by the lifecycle proxy, so 1..10
    images are all valid. The instruction is passed through unchanged; input
    alpha is restored before Qwen encoding.

    Returns the saved result path(s) and the image. The output follows the
    input image's aspect ratio within the workflow's encode resolution budget
    (~1536² ≈ 2.36 MP, rounded to a multiple of 32).
    """
    image_path = _local_image_path(image_path)
    if not os.path.isfile(image_path):
        raise RuntimeError(f"no such file: {image_path}")
    refs = [_local_image_path(path) for path in (reference_images or [])]
    for ref in refs:
        if not os.path.isfile(ref):
            raise RuntimeError(f"no such reference image: {ref}")
    if len(refs) > 9:
        raise RuntimeError("at most 9 reference_images are supported")
    if not _VIDEO_LOCK.acquire(blocking=False):
        raise RuntimeError("another generation is already running; wait for it to finish")
    try:
        uploads = [_upload_image_file(p) for p in [image_path] + refs]
        gen_seed = seed if seed is not None else random.randint(1, 2**31)
        entry, save_node = _edit_once(uploads, prompt, steps, gen_seed)
        note = f"Edited {os.path.basename(image_path)} (Turbo, 8-step schedule; seed {gen_seed})."
        return _image_result(_collect_images(entry, save_node), note)
    finally:
        _VIDEO_LOCK.release()


@server.tool()
def generate_video(
    prompt: str,
    quality: str = "draft",
    size: int | None = None,
    seconds: float | None = None,
    seed: int | None = None,
    aspect_ratio: str = "1:1",
) -> list:
    """Generate an LTX-2.5 video clip from a text prompt.

    Two pipelines share one workflow:
      - `draft` (default): one 512x512 pass, 49 frames (~2 s) by default.
      - `final`: staged — half-size base, 2x latent upscale, three
        refinement steps, HQ VAE decode; 1024x1024 by default, or the
        official 1280x704 with size=1280 and aspect_ratio="16:9".

    The text encoder (Gemma 4, ~12.8 GB) and the video DiT (~21.5 GB) cannot
    share 31 GB of host RAM, so this tool encodes the prompt first, asks the
    lifecycle proxy to recycle the ComfyUI process, then generates. Encoded
    prompts are cached in `models/embeddings`, so repeating a prompt skips the
    encoder (~1 minute saved). `quality='final'` recycles between its four
    phases; expect roughly 5-6 minutes including cold starts for short clips,
    and 6-8 minutes at 10 s.

    `aspect_ratio` is '1:1' (compatible square default), '16:9' (landscape),
    or '9:16' (portrait). `size` is the longest edge: default 512 draft or
    1024 final, 256..1280 divisible by 64. Landscape defaults are 512x288 draft
    and 1024x576 final; `size=1280` with 16:9 gives the official 1280x704
    final. Other sizes are rounded to the nearest VAE-aligned short edge
    (32 draft / 64 final).
    `seconds` is rounded to the nearest valid LTX length (8n+1 frames) and
    capped at 10 s (241 frames). Above 6 s the draft pixel budget is capped at
    512x512 so the fast path stays fast; final keeps its full size. Renders are
    capped at ~1 MP: `size=1280` with `aspect_ratio="16:9"` gives the official
    1280x704 geometry (10 s measured 431 s), while `size=1024` is the square
    maximum. `seed` defaults to random. LTX also
    generates audio (quiet ambience unless the prompt describes sound).

    Returns the MP4 path and a poster frame. The GPU is single-tenant, so a
    video job blocks image generation until it finishes.
    """
    quality = quality.strip().lower()
    if quality not in {"draft", "final"}:
        raise RuntimeError("quality must be 'draft' or 'final'")
    width, height = _video_dimensions(quality, size, aspect_ratio)
    if seconds is None:
        seconds = 2.0 if quality == "draft" else 3.4
    if not 0.5 <= seconds <= 10.0:
        raise RuntimeError("seconds must be between 0.5 and 10.0")
    _check_long_clip(seconds, quality, width, height)
    frames = _video_frames(seconds)
    gen_seed = seed if seed is not None else random.randint(1, 2**31)
    if not _VIDEO_LOCK.acquire(blocking=False):
        raise RuntimeError("another video generation is already running; wait for it to finish")
    try:
        result = _generate_video(
            prompt, quality == "final", width, height, frames, gen_seed, quality
        )
        return _format_video_result(result)
    finally:
        _VIDEO_LOCK.release()


@server.tool()
def generate_video_from_image(
    image_path: str,
    prompt: str,
    quality: str = "draft",
    size: int | None = None,
    seconds: float | None = None,
    strength: float = 1.0,
    seed: int | None = None,
    aspect_ratio: str = "1:1",
) -> list:
    """Animate a still image with LTX-2.5 (first-frame image-to-video).

    `image_path` is a path this server can read: any host path for the stdio
    server, or a file directly under `/comfyui/input` for the shared container.
    The image is uploaded to ComfyUI (or reused in place if it is already in
    the shared input directory) and used as the first frame; `prompt` describes
    the motion, camera, and sound. `strength` 1.0 keeps the first frame hard,
    lower values let the model deviate more.

    Uses the same pipelines and aspect_ratio presets as `generate_video`:
    `draft` is a 512-long-edge single pass, `final` the staged pipeline
    (1024 square by default, or the official 1280x704 with size=1280 and
    aspect_ratio="16:9").
    `seconds` is capped at 10 s with the same >6 s draft budget as
    `generate_video` (final keeps its full size).
    The source image is fitted by LTX to the selected frame dimensions.
    The final base phase additionally
    loads the conv video VAE to encode the image; if memory is tight, use
    draft. This is first-frame conditioning, not Qwen-style multi-reference
    editing: LTX has no identity-reference encoder.
    """
    image_path = _local_image_path(image_path)
    if not os.path.isfile(image_path):
        raise RuntimeError(f"no such file: {image_path}")
    if not 0.0 <= strength <= 1.0:
        raise RuntimeError("strength must be between 0 and 1")
    quality = quality.strip().lower()
    if quality not in {"draft", "final"}:
        raise RuntimeError("quality must be 'draft' or 'final'")
    width, height = _video_dimensions(quality, size, aspect_ratio)
    if seconds is None:
        seconds = 2.0 if quality == "draft" else 3.4
    if not 0.5 <= seconds <= 10.0:
        raise RuntimeError("seconds must be between 0.5 and 10.0")
    _check_long_clip(seconds, quality, width, height)
    frames = _video_frames(seconds)
    gen_seed = seed if seed is not None else random.randint(1, 2**31)
    if not _VIDEO_LOCK.acquire(blocking=False):
        raise RuntimeError("another video generation is already running; wait for it to finish")
    try:
        result = _generate_video(
            prompt, quality == "final", width, height, frames, gen_seed, quality,
            image_path=image_path, strength=strength,
        )
        return _format_video_result(result)
    finally:
        _VIDEO_LOCK.release()


@server.tool()
def generate_gif(
    prompt: str,
    size: int = 384,
    seconds: float = 1.0,
    width: int = 360,
    fps: int = 8,
    colors: int = 128,
    seed: int | None = None,
) -> list:
    """Generate a short looping GIF for chat, reactions, and memes.

    This is a delivery preset, not a separate model: LTX renders a 384x384
    clip, then the frames are decimated to `fps`, quantized, and saved as a
    looping GIF. Defaults are tuned for meme/reaction use and for MCP clients
    with a 1 MB SSE event limit: 25 generated frames (1.04 s), 9 GIF frames at
    8 fps, 360 px wide, 128 colors. The inline base64 stays under the limit;
    raise `width`, `fps`, or `colors` for a sharper file, or lower them for a
    smaller one.

    `size` is 256..512 and divisible by 64; `seconds` is 0.5..2.0 (rounded to
    8n+1 frames); `width` is 240..640 and even; `fps` is 8..24; `colors` is
    32..256. The GIF has no audio and is looped forever. It is returned inline
    as image/gif, plus the source MP4 path. Expect ~1 minute with cached
    conditioning and ~2 minutes for a new prompt (the text encoder dominates).
    """
    if size % 64 or not 256 <= size <= 512:
        raise RuntimeError("size must be 256..512 and divisible by 64")
    if not 0.5 <= seconds <= 2.0:
        raise RuntimeError("seconds must be between 0.5 and 2.0")
    if width % 2 or not 240 <= width <= 640:
        raise RuntimeError("width must be 240..640 and even")
    if not 8 <= fps <= 24:
        raise RuntimeError("fps must be between 8 and 24")
    if not 32 <= colors <= 256:
        raise RuntimeError("colors must be between 32 and 256")
    frames = _video_frames(seconds)
    gen_seed = seed if seed is not None else random.randint(1, 2**31)
    if not _VIDEO_LOCK.acquire(blocking=False):
        raise RuntimeError("another video generation is already running; wait for it to finish")
    try:
        result = _generate_video(prompt, False, size, size, frames, gen_seed, "gif")
        gif_path = os.path.splitext(result["video"])[0] + ".gif"
        count, duration = _mp4_to_gif(result["video"], gif_path, width, fps, colors)
        rel = os.path.relpath(gif_path, OUTPUT_DIR)
        gif_item = {"filename": os.path.basename(rel), "subfolder": os.path.dirname(rel)}
        note = (
            f"{result['note']} GIF {width}px, {count} frames at {fps} fps, "
            f"{colors} colors ({duration} ms), looped."
        )
        text = (
            f"{note}\n- video: {result['video_host']}\n"
            f"- gif: {os.path.join(OUTPUT_HOST_DIR, rel)}\n"
            f"- gif url: {_media_url(gif_item)}"
        )
        if os.path.getsize(gif_path) > INLINE_IMAGE_BYTES:
            return [text + "\nGIF exceeds the inline MCP size budget; open the GIF URL.",
                    _inline_still(result["poster"])]
        with open(gif_path, "rb") as fh:
            return [text, Image(data=fh.read(), format="gif")]
    finally:
        _VIDEO_LOCK.release()


if __name__ == "__main__":
    transport = os.environ.get("COMFYUI_MCP_TRANSPORT", "stdio").strip().lower()
    if transport == "stdio":
        server.run("stdio")
    elif transport in {"streamable-http", "sse"}:
        server.run(
            transport,
            host=os.environ.get("COMFYUI_MCP_HOST", "0.0.0.0"),
            port=int(os.environ.get("COMFYUI_MCP_PORT", "8000")),
        )
    else:
        raise SystemExit(f"unknown COMFYUI_MCP_TRANSPORT: {transport}")
