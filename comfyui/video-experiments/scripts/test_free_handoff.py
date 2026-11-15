#!/usr/bin/env python3
"""Validate the /free-only handoff: encode a prompt, unload, then generate video.

No container restart and no lifecycle calls. This is the handoff the MCP server
would use, so success here means the MCP can own the two passes itself.
"""
import argparse
import fcntl
import hashlib
import json
import shutil
import time
import uuid

from run_video import (
    BASE, EXP, EXP_OUTPUT, PROXY, http_json, patch_workflow,
    extract_saved_video, convert_gif,
)


def mem_available_mb() -> int:
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    raise RuntimeError("MemAvailable missing")


def wait(pid: str, timeout: int = 1200) -> dict:
    start = time.time()
    while time.time() - start < timeout:
        entry = http_json(f"{PROXY}/history/{pid}", timeout=60).get(pid)
        if entry:
            status = entry.get("status", {})
            if status.get("status_str") == "error":
                raise RuntimeError(json.dumps(status)[:2000])
            if status.get("completed"):
                return entry
        time.sleep(5)
    raise TimeoutError(f"still pending: {pid}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--size", type=int, default=512)
    ap.add_argument("--frames", type=int, default=81)
    ap.add_argument("--seed", type=int, default=20261115)
    args = ap.parse_args()

    lock = open("/tmp/opencode/comfyui-video-gpu.lock", "a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    digest = hashlib.sha256(args.prompt.encode()).hexdigest()[:16]
    name = f"ltx25_free_{digest}.safetensors"
    target = BASE / "models" / "embeddings" / name
    prefix = f"video-experiments/conditioning/free_{uuid.uuid4().hex}"
    report = {"prompt": args.prompt, "conditioning": name, "phases": []}

    print(f"start MemAvailable={mem_available_mb()} MB", flush=True)

    # Pass 1: text encoder only.
    encode_graph = {
        "1": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": "gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors",
            "type": "ltxv", "device": "default"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["1", 0], "text": args.prompt}},
        "3": {"class_type": "SaveConditioning", "inputs": {"conditioning": ["2", 0], "filename_prefix": prefix}},
    }
    t0 = time.time()
    pid = http_json(f"{PROXY}/prompt", {"prompt": encode_graph, "client_id": f"free-{digest}"}, timeout=300)["prompt_id"]
    wait(pid)
    saved = list((BASE / "output").glob(prefix + "_*.safetensors"))
    if len(saved) != 1:
        raise RuntimeError(f"expected one conditioning file, got {saved}")
    shutil.copy2(saved[0], target)
    report["phases"].append({
        "phase": "encode", "prompt_id": pid, "seconds": round(time.time() - t0, 1),
        "mem_available_mb": mem_available_mb(), "bytes": target.stat().st_size,
    })
    print(f"encode done in {report['phases'][-1]['seconds']}s; "
          f"MemAvailable={mem_available_mb()} MB", flush=True)

    # The handoff under test: unload models, no restart.
    t1 = time.time()
    http_json(f"{PROXY}/free", {"unload_models": True, "free_memory": True}, timeout=120)
    time.sleep(3)
    report["phases"].append({
        "phase": "free", "seconds": round(time.time() - t1, 1),
        "mem_available_mb": mem_available_mb(),
    })
    print(f"after /free: MemAvailable={mem_available_mb()} MB", flush=True)

    # Pass 2: video graph, no text encoder.
    settings = argparse.Namespace(
        prompt=args.prompt, negative="", seed=args.seed, width=args.size,
        height=args.size, frames=args.frames, fps=24, steps=8, cfg=1.0,
        cached_conditioning=name, refine=False,
    )
    graph, applied = patch_workflow("ltx", settings)
    t2 = time.time()
    pid2 = http_json(f"{PROXY}/prompt", {"prompt": graph, "client_id": f"free-gen-{digest}"}, timeout=300)["prompt_id"]
    entry = wait(pid2)
    item = extract_saved_video({pid2: entry}, pid2, "ltx")
    source = BASE / "output" / item.get("subfolder", "") / item["filename"]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    stem = f"ltx_free_handoff_{args.size}x{args.size}_{args.frames}f_{stamp}"
    mp4 = EXP_OUTPUT / f"{stem}.mp4"
    shutil.copy2(source, mp4)
    ok, msg = convert_gif(
        f"/opt/ComfyUI/output/{item.get('subfolder', '')}/{item['filename']}",
        f"/opt/ComfyUI/output/{item.get('subfolder', '')}/{stem}.gif",
        EXP / "scripts" / "mp4_to_gif.py", 640,
    )
    if ok:
        shutil.copy2(BASE / "output" / item.get("subfolder", "") / f"{stem}.gif",
                     EXP_OUTPUT / f"{stem}.gif")
    report["phases"].append({
        "phase": "generate", "prompt_id": pid2, "seconds": round(time.time() - t2, 1),
        "mem_available_mb": mem_available_mb(), "mp4": str(mp4),
        "mp4_bytes": mp4.stat().st_size, "gif_ok": ok, "gif_message": msg,
        "settings": applied,
    })
    report["ok"] = True
    (EXP_OUTPUT / f"{stem}.json").write_text(json.dumps(report, indent=2))
    print(f"SUCCESS {mp4} in {report['phases'][-1]['seconds']}s; gif={ok}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
