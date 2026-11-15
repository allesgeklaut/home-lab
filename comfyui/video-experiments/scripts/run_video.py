#!/usr/bin/env python3
"""Submit one video workflow through the ComfyUI lifecycle proxy and collect output.

Usage:
  python3 run_video.py --family ltx --cached-conditioning saved_prompt.safetensors

The script never starts/stops the ComfyUI container itself. It only talks to
http://127.0.0.1:8189, the lifecycle proxy, which owns start/stop. Status
polling also keeps the proxy's 900 s idle timer fresh during long generations.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

BASE = Path("/opt/stacks/comfyui")
EXP = BASE / "video-experiments"
PROXY = os.environ.get("COMFYUI_PROXY", "http://127.0.0.1:8189")
OUTPUT_DIR = BASE / "output"
EXP_OUTPUT = EXP / "output"
LOG_DIR = EXP / "logs"

WORKFLOWS = {
    "ltx": EXP / "workflows" / "ltx25_distilled_t2v_api.json",
}

DEFAULT_PROMPT = (
    "A magical hand-painted storybook animation at night. A huge ivory and warm "
    "ochre origami paper whale decorated with delicate ink map lines floats slowly "
    "from left to right above a mountain observatory. Its tail and fins gently "
    "undulate. Warm amber light glows from observatory windows and a copper dome on "
    "a rocky cliff. Distant snowy alpine peaks and dark pine trees below. Soft "
    "clouds drift slowly, tiny stars softly twinkle in a deep navy Milky Way sky. "
    "Quiet dreamy atmosphere, textured ink and watercolor illustration, fixed wide "
    "camera, stable scenery, restrained smooth motion, no text or captions."
)
DEFAULT_NEGATIVE = (
    "text, captions, watermark, people, camera movement, pan, zoom, jitter, "
    "flicker, blurry, low quality, distorted"
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def http_json(url: str, payload: dict | None = None, timeout: int = 300) -> dict:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
    return json.loads(body) if body else {}


def workflow_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def patch_workflow(family: str, args: argparse.Namespace) -> tuple[dict, dict]:
    path = WORKFLOWS[family]
    graph = json.loads(path.read_text())
    applied = {
        "workflow": str(path),
        "workflow_sha256_16": workflow_sha(path),
        "prompt": args.prompt,
        "negative": args.negative,
        "seed": args.seed,
        "width": args.width,
        "height": args.height,
        "frames": args.frames,
        "fps": args.fps,
        "steps": args.steps,
        "cfg": args.cfg,
    }

    if family == "ltx":
        graph["3"]["inputs"]["text"] = args.prompt
        graph["4"]["inputs"]["text"] = args.negative
        graph["5"]["inputs"].update(
            width=args.width, height=args.height, length=args.frames, batch_size=1
        )
        graph["6"]["inputs"]["frame_rate"] = float(args.fps)
        graph["14"]["inputs"]["noise_seed"] = args.seed
        graph["17"]["inputs"].update(frames_number=args.frames, frame_rate=float(args.fps))
        graph["12"]["inputs"]["filename_prefix"] = f"video-experiments/ltx25_distilled_{args.seed}"
        cached = getattr(args, "cached_conditioning", None)
        if cached:
            del graph["2"]
            graph["3"] = {
                "class_type": "ConditioningLoader",
                "inputs": {"conditioning_name": cached},
            }
            graph["4"] = {
                "class_type": "ConditioningZeroOut",
                "inputs": {"conditioning": ["3", 0]},
            }
            applied["cached_conditioning"] = cached
        if getattr(args, "refine", False):
            # Official two-stage recipe: half-size generation, latent 2x
            # upscaling, then three refinement steps at the requested output size.
            graph["5"]["inputs"].update(width=args.width // 2, height=args.height // 2)
            graph["15"]["inputs"]["vae_name"] = "ltx-2.5-video-vae-bf16.safetensors"
            graph.update({
                "21": {"class_type": "LatentUpscaleModelLoader", "inputs": {
                    "model_name": "ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors"}},
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
            graph["12"]["inputs"]["filename_prefix"] = f"video-experiments/ltx25_refined_{args.seed}"
            applied.update(
                first_stage_width=args.width // 2, first_stage_height=args.height // 2,
                refinement_steps=3, refinement_seed=42,
                video_vae="ltx-2.5-video-vae-bf16.safetensors",
            )
    else:
        raise SystemExit(f"unknown family {family}")

    graph["11"]["inputs"]["fps"] = float(args.fps)
    return graph, applied

def extract_saved_video(history: dict, prompt_id: str, family: str) -> dict:
    entry = history.get(prompt_id) or {}
    outputs = entry.get("outputs") or {}
    for node_id, node_out in outputs.items():
        for item in node_out.get("images", []):
            if str(item.get("filename", "")).lower().endswith((".mp4", ".webm", ".mkv")):
                return item
    raise RuntimeError(f"no saved video in history outputs: {json.dumps(outputs)[:1000]}")


def convert_gif(container_src: str, container_dst: str, script: Path, max_width: int) -> tuple[bool, str]:
    try:
        with script.open("rb") as fh:
            proc = subprocess.run(
                ["docker", "exec", "-i", "comfyui", "python3", "-", container_src, container_dst, str(max_width)],
                stdin=fh,
                capture_output=True,
                text=True,
                timeout=600,
            )
        if proc.returncode != 0:
            return False, (proc.stderr or proc.stdout or "docker exec failed").strip()[-1500:]
        return True, proc.stdout.strip()
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--family", choices=sorted(WORKFLOWS), required=True)
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    ap.add_argument("--negative", default=DEFAULT_NEGATIVE)
    ap.add_argument("--seed", type=int, default=20261115)
    ap.add_argument("--width", type=int, default=None)
    ap.add_argument("--height", type=int, default=None)
    ap.add_argument("--frames", type=int, default=49)
    ap.add_argument("--fps", type=int, default=24)
    ap.add_argument("--steps", type=int, default=None)
    ap.add_argument("--cfg", type=float, default=None)
    ap.add_argument("--timeout", type=int, default=3600)
    ap.add_argument("--poll", type=int, default=5)
    ap.add_argument("--no-gif", action="store_true")
    ap.add_argument("--gif-width", type=int, default=640)
    ap.add_argument("--cached-conditioning", help="LTX: saved prompt under models/embeddings; avoids loading Gemma.")
    ap.add_argument("--refine", action="store_true", help="LTX: official 2x latent upscale + 3-step refinement and higher-quality decoder. Width/height are final size.")
    ap.add_argument("--min-available-mb", type=int, default=0, help="Abort our exclusive job if host MemAvailable drops below this safety margin.")
    args = ap.parse_args()

    if args.width is None:
        args.width = 512
    if args.height is None:
        args.height = 512
    if args.steps is None:
        args.steps = 8
    if args.cfg is None:
        args.cfg = 1.0
    if args.family == "ltx" and (args.steps != 8 or args.cfg != 1.0):
        ap.error("LTX-2.5 distilled uses the fixed official 8-step schedule and CFG 1.")
    if args.width % 32 or args.height % 32 or args.frames % 8 != 1:
        ap.error("Video dimensions must be multiples of 32 and frames must equal 8n+1.")
    if args.refine and (args.family != "ltx" or args.width % 64 or args.height % 64):
        ap.error("--refine requires LTX and final dimensions divisible by 64.")

    lock = open("/tmp/opencode/comfyui-video-gpu.lock", "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        ap.error("Another video experiment holds the GPU lock.")

    EXP_OUTPUT.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    graph, applied = patch_workflow(args.family, args)
    applied["resolved_graph"] = graph
    client_id = f"video-exp-{uuid.uuid4().hex[:12]}"
    print(f"[{now()}] family={args.family} proxy={PROXY}", flush=True)
    print(f"[{now()}] settings={json.dumps(applied, sort_keys=True)}", flush=True)

    stats_before = {}
    try:
        stats_before = http_json(f"{PROXY}/system_stats", timeout=15)
    except Exception as exc:  # noqa: BLE001
        print(f"[{now()}] system_stats before failed: {exc}", flush=True)

    t_submit = time.time()
    try:
        resp = http_json(
            f"{PROXY}/prompt",
            {"prompt": graph, "client_id": client_id},
            timeout=args.timeout,
        )
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        print(f"[{now()}] POST /prompt HTTP {exc.code}: {body[:4000]}", flush=True)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"[{now()}] POST /prompt failed: {type(exc).__name__}: {exc}", flush=True)
        return 2

    prompt_id = resp.get("prompt_id")
    if not prompt_id:
        print(f"[{now()}] no prompt_id in response: {json.dumps(resp)[:2000]}", flush=True)
        return 2
    print(f"[{now()}] queued prompt_id={prompt_id} number={resp.get('number')}", flush=True)

    history = {}
    last_state = None
    while True:
        elapsed = time.time() - t_submit
        if args.min_available_mb:
            mem = {
                line.split(":")[0]: int(line.split()[1])
                for line in Path("/proc/meminfo").read_text().splitlines()
                if line.startswith(("MemAvailable:", "SwapFree:"))
            }
            if mem["MemAvailable"] < args.min_available_mb * 1024:
                queue = http_json(f"{PROXY}/queue", timeout=15)
                running = [job[1] for job in queue.get("queue_running", [])]
                if running == [prompt_id] and not queue.get("queue_pending"):
                    print(f"[{now()}] MEMORY SAFETY STOP: {mem}", flush=True)
                    http_json(f"{PROXY}/interrupt", {}, timeout=15)
                    # Model loading may not heed interrupt until allocation ends.
                    # The proxy re-checks that the queue is ours before stopping.
                    try:
                        http_json(f"{PROXY}/recycle", {"prompt_id": prompt_id}, timeout=180)
                    except Exception as exc:
                        print(f"[{now()}] recycle failed: {exc}", flush=True)
                    (LOG_DIR / f"{prompt_id}_memory_abort.json").write_text(
                        json.dumps({"prompt_id": prompt_id, "memory_kb": mem, "settings": applied}, indent=2)
                    )
                    return 6
        if elapsed > args.timeout:
            print(f"[{now()}] TIMEOUT after {elapsed:.0f}s", flush=True)
            return 3
        try:
            history = http_json(f"{PROXY}/history/{prompt_id}", timeout=60)
        except Exception as exc:  # noqa: BLE001
            print(f"[{now()}] history poll error: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(args.poll)
            continue

        entry = history.get(prompt_id)
        if entry:
            status = entry.get("status") or {}
            state = (status.get("status_str"), status.get("completed"))
            if state != last_state:
                print(f"[{now()}] status={state} elapsed={elapsed:.0f}s", flush=True)
                last_state = state
            if status.get("completed") or status.get("status_str") in ("success", "error"):
                break
        else:
            if last_state != "waiting":
                print(f"[{now()}] waiting for history entry elapsed={elapsed:.0f}s", flush=True)
                last_state = "waiting"
        time.sleep(args.poll)

    t_done = time.time()
    entry = history.get(prompt_id) or {}
    status = entry.get("status") or {}
    if status.get("status_str") == "error":
        print(f"[{now()}] generation failed: {json.dumps(status)[:4000]}", flush=True)
        return 4

    item = extract_saved_video(history, prompt_id, args.family)
    subfolder = item.get("subfolder", "")
    filename = item["filename"]
    host_src = OUTPUT_DIR / subfolder / filename
    if not host_src.exists():
        print(f"[{now()}] output missing on host: {host_src}", flush=True)
        return 5

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    stem = f"{args.family}_{args.seed}_{args.width}x{args.height}_{args.frames}f_{stamp}"
    host_mp4 = EXP_OUTPUT / f"{stem}.mp4"
    shutil.copy2(host_src, host_mp4)
    print(f"[{now()}] mp4={host_mp4} ({host_mp4.stat().st_size} bytes)", flush=True)

    gif_info = {"attempted": False, "ok": False}
    host_gif = EXP_OUTPUT / f"{stem}.gif"
    if not args.no_gif:
        gif_info["attempted"] = True
        container_src = f"/opt/ComfyUI/output/{subfolder}/{filename}"
        container_dst = f"/opt/ComfyUI/output/{subfolder}/{stem}.gif"
        ok, msg = convert_gif(
            container_src, container_dst, EXP / "scripts" / "mp4_to_gif.py", args.gif_width
        )
        gif_info.update(ok=ok, message=msg)
        container_gif = OUTPUT_DIR / subfolder / f"{stem}.gif"
        if ok and container_gif.exists():
            shutil.copy2(container_gif, host_gif)
            gif_info["path"] = str(host_gif)
            gif_info["bytes"] = host_gif.stat().st_size
            print(f"[{now()}] gif={host_gif} ({host_gif.stat().st_size} bytes)", flush=True)
        else:
            print(f"[{now()}] gif conversion failed: {msg}", flush=True)

    stats_after = {}
    try:
        stats_after = http_json(f"{PROXY}/system_stats", timeout=15)
    except Exception:  # noqa: BLE001
        pass

    meta = {
        "family": args.family,
        "prompt_id": prompt_id,
        "client_id": client_id,
        "submitted_utc": datetime.fromtimestamp(t_submit, timezone.utc).isoformat(timespec="seconds"),
        "completed_utc": datetime.fromtimestamp(t_done, timezone.utc).isoformat(timespec="seconds"),
        "wall_seconds": round(t_done - t_submit, 1),
        "settings": applied,
        "status": status,
        "saved": item,
        "host_mp4": str(host_mp4),
        "mp4_bytes": host_mp4.stat().st_size,
        "gif": gif_info,
        "system_stats_before": stats_before,
        "system_stats_after": stats_after,
    }
    meta_path = EXP_OUTPUT / f"{stem}.json"
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    print(f"[{now()}] metadata={meta_path}", flush=True)
    print(f"[{now()}] DONE wall={t_done - t_submit:.1f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
