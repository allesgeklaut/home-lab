#!/usr/bin/env python3
"""Cache LTX prompt through native ComfyUI nodes, without loading its video model."""
import argparse
import fcntl
import hashlib
import json
import shutil
import time
import uuid

from run_video import BASE, EXP, PROXY, http_json


def main():
    ap = argparse.ArgumentParser()
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument("--source-log", help="Original runner log; reuse its exact prompt/settings.")
    source.add_argument("--prompt", help="New prompt text to encode.")
    ap.add_argument("--manifest", default="ltx25_cached_prompt.json", help="Manifest filename under output/.")
    args = ap.parse_args()
    if args.prompt:
        prompt = args.prompt
        settings = {"prompt": prompt, "source": "explicit --prompt"}
    else:
        with open(args.source_log) as f:
            settings = next(json.loads(line.split("settings=", 1)[1]) for line in f if "settings=" in line)
        prompt = settings["prompt"]
    digest = hashlib.sha256(prompt.encode()).hexdigest()[:16]
    filename = f"ltx25_whale_{digest}.safetensors"
    target = BASE / "models" / "embeddings" / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    manifest = EXP / "output" / args.manifest
    lock = open("/tmp/opencode/comfyui-video-gpu.lock", "a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    prefix = f"video-experiments/conditioning/ltx25_{uuid.uuid4().hex}"
    graph = {
        "1": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": "gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors",
            "type": "ltxv", "device": "default"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["1", 0], "text": prompt}},
        "3": {"class_type": "SaveConditioning", "inputs": {"conditioning": ["2", 0], "filename_prefix": prefix}},
    }
    start = time.time()
    result = http_json(f"{PROXY}/prompt", {"prompt": graph, "client_id": f"cache-{digest}"}, timeout=300)
    pid = result["prompt_id"]
    print(f"Encoding only: {pid}", flush=True)
    while time.time() - start < 900:
        entry = http_json(f"{PROXY}/history/{pid}", timeout=60).get(pid)
        if entry:
            status = entry.get("status", {})
            if status.get("status_str") == "error":
                raise RuntimeError(json.dumps(status))
            if status.get("completed"):
                break
        time.sleep(5)
    else:
        raise TimeoutError(f"Encoding still pending: {pid}; do not submit another job.")
    files = list((BASE / "output").glob(prefix + "_*.safetensors"))
    if len(files) != 1:
        raise RuntimeError(f"Expected one saved conditioning, got {files}")
    shutil.copy2(files[0], target)
    # No other job is submitted while our lock is held. Release encoder weights.
    http_json(f"{PROXY}/free", {"unload_models": True, "free_memory": True})
    manifest.write_text(json.dumps({
        "conditioning_name": filename, "prompt": prompt, "source_settings": settings,
        "encoding_prompt_id": pid, "encoding_seconds": round(time.time() - start, 1),
        "bytes": target.stat().st_size,
    }, indent=2) + "\n")
    print(f"Saved {target} ({target.stat().st_size} bytes); encoder unloaded.", flush=True)


if __name__ == "__main__":
    main()
