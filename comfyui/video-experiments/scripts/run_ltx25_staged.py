#!/usr/bin/env python3
"""Run native LTX refinement in isolated processes to avoid model overlap.

Each boundary uses native SaveLatent / LoadLatent, with video and audio saved
separately. No reference image, custom node, or Python package is required.
"""
import argparse
import fcntl
import json
import shutil
import time
import uuid
from datetime import datetime

from run_video import (
    BASE, EXP, PROXY, OUTPUT_DIR, EXP_OUTPUT, http_json, patch_workflow,
    extract_saved_video, convert_gif,
)


def ancestors(graph, outputs):
    selected = {}

    def visit(nid):
        if nid in selected:
            return
        node = graph[nid]
        selected[nid] = node
        for value in node["inputs"].values():
            if isinstance(value, list) and len(value) == 2:
                visit(value[0])

    for nid in outputs:
        visit(nid)
    return selected


def restart_idle():
    try:
        q = http_json(f"{PROXY}/queue", timeout=15)
        if q.get("queue_running") or q.get("queue_pending"):
            raise RuntimeError("Queue busy: refusing to restart ComfyUI.")
    except Exception:
        # A stopped backend legitimately returns 503 for /queue.
        if http_json(f"{PROXY}/health", timeout=15).get("comfyui") != "down":
            raise
    http_json(f"{PROXY}/recycle", {}, timeout=180)


def run_job(graph, name, records):
    start = time.time()
    pid = http_json(f"{PROXY}/prompt", {"prompt": graph, "client_id": name}, timeout=300)["prompt_id"]
    print(f"{name}: queued {pid}", flush=True)
    records.append({"phase": name, "prompt_id": pid, "graph": graph})
    while time.time() - start < 1200:
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith(("MemAvailable:", "SwapFree:")):
                    mem[line.split(":")[0]] = int(line.split()[1])
        if mem["MemAvailable"] < 1024 * 1024:
            q = http_json(f"{PROXY}/queue", timeout=15)
            if [x[1] for x in q.get("queue_running", [])] == [pid] and not q.get("queue_pending"):
                http_json(f"{PROXY}/interrupt", {}, timeout=15)
                try:
                    http_json(f"{PROXY}/recycle", {"prompt_id": pid}, timeout=180)
                except Exception as exc:
                    print(f"recycle after memory stop failed: {exc}", flush=True)
                raise RuntimeError(f"Memory safety stop in {name}: {mem}")
        entry = http_json(f"{PROXY}/history/{pid}", timeout=60).get(pid)
        if entry:
            status = entry.get("status", {})
            if status.get("status_str") == "error":
                raise RuntimeError(json.dumps(status))
            if status.get("completed"):
                records[-1].update(seconds=round(time.time() - start, 1), status=status)
                print(f"{name}: complete in {records[-1]['seconds']}s", flush=True)
                return entry
        time.sleep(5)
    raise TimeoutError(f"{name} still pending: {pid}; do not submit another job.")


def save_node(link, prefix):
    return {"class_type": "SaveLatent", "inputs": {"samples": link, "filename_prefix": prefix}}


def load_node(name):
    return {"class_type": "LoadLatent", "inputs": {"latent": name}}


def copy_latent(entry, node_id, run_id, label):
    items = entry["outputs"][node_id]["latents"]
    if len(items) != 1:
        raise RuntimeError(f"Expected one latent for {label}: {items}")
    item = items[0]
    source = OUTPUT_DIR / item["subfolder"] / item["filename"]
    filename = f"{run_id}_{label}.latent"
    shutil.copy2(source, BASE / "input" / filename)
    print(f"Cached {label}: {source.stat().st_size} bytes", flush=True)
    return filename


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", type=int, default=1024)
    ap.add_argument("--manifest", default="ltx25_cached_prompt.json")
    ap.add_argument("--tag", default="refined")
    args = ap.parse_args()
    if args.size % 64:
        ap.error("Final size must be divisible by 64.")
    lock = open("/tmp/opencode/comfyui-video-gpu.lock", "a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    manifest = json.loads((EXP_OUTPUT / args.manifest).read_text())
    settings = argparse.Namespace(
        prompt=manifest["prompt"], negative="", seed=20261115, width=args.size,
        height=args.size, frames=81, fps=24, steps=8, cfg=1.0,
        cached_conditioning=manifest["conditioning_name"], refine=True,
    )
    graph, applied = patch_workflow("ltx", settings)
    run_id = f"ltx25_staged_{uuid.uuid4().hex[:10]}"
    prefix = f"video-experiments/staged/{run_id}"
    records = []
    report = EXP_OUTPUT / f"{run_id}_phases.json"
    start = time.time()
    try:
        # Phase 1: DiT only; no video VAE or upscaler.
        graph["30"] = save_node(["19", 0], prefix + "_base_video")
        graph["31"] = save_node(["19", 1], prefix + "_base_audio")
        restart_idle()
        entry = run_job(ancestors(graph, ["30", "31"]), "base", records)
        video = copy_latent(entry, "30", run_id, "base_video")
        audio = copy_latent(entry, "31", run_id, "base_audio")
        report.write_text(json.dumps({"settings": applied, "phases": records}, indent=2))

        # Phase 2: upscaler only; no DiT or text encoder.
        graph["28"] = load_node(video)
        graph["22"]["inputs"]["samples"] = ["28", 0]
        graph["30"] = save_node(["22", 0], prefix + "_upscaled_video")
        restart_idle()
        entry = run_job(ancestors(graph, ["30"]), "upscale", records)
        upscaled = copy_latent(entry, "30", run_id, "upscaled_video")
        report.write_text(json.dumps({"settings": applied, "phases": records}, indent=2))

        # Phase 3: DiT only at final resolution, official three-step sigmas.
        graph["22"] = load_node(upscaled)
        graph["28"] = load_node(audio)
        graph["23"]["inputs"]["audio_latent"] = ["28", 0]
        graph["30"] = save_node(["27", 0], prefix + "_refined_video")
        graph["31"] = save_node(["27", 1], prefix + "_refined_audio")
        restart_idle()
        entry = run_job(ancestors(graph, ["30", "31"]), "refine", records)
        refined_video = copy_latent(entry, "30", run_id, "refined_video")
        refined_audio = copy_latent(entry, "31", run_id, "refined_audio")
        report.write_text(json.dumps({"settings": applied, "phases": records}, indent=2))

        # Phase 4: decoders only; no DiT, upscaler or text encoder.
        graph["28"] = load_node(refined_video)
        graph["29"] = load_node(refined_audio)
        graph["10"]["inputs"]["samples"] = ["28", 0]
        graph["20"]["inputs"]["samples"] = ["29", 0]
        graph["12"]["inputs"]["filename_prefix"] = f"video-experiments/{run_id}"
        restart_idle()
        entry = run_job(ancestors(graph, ["12"]), "decode", records)
        pid = records[-1]["prompt_id"]
        item = extract_saved_video({pid: entry}, pid, "ltx")
        source = OUTPUT_DIR / item["subfolder"] / item["filename"]
        stem = f"ltx_{args.tag}_20261115_{args.size}x{args.size}_81f_{datetime.now():%Y%m%d-%H%M%S}"
        target = EXP_OUTPUT / f"{stem}.mp4"
        shutil.copy2(source, target)
        src = f"/opt/ComfyUI/output/{item['subfolder']}/{item['filename']}"
        dst = f"/opt/ComfyUI/output/{item['subfolder']}/{stem}.gif"
        ok, msg = convert_gif(src, dst, EXP / "scripts/mp4_to_gif.py", 640)
        if ok:
            shutil.copy2(OUTPUT_DIR / item["subfolder"] / f"{stem}.gif", EXP_OUTPUT / f"{stem}.gif")
        report.write_text(json.dumps({
            "settings": applied, "phases": records, "mp4": str(target),
            "gif_ok": ok, "gif_message": msg, "seconds": round(time.time() - start, 1),
        }, indent=2))
        print(f"SUCCESS: {target}; GIF={ok}; {time.time() - start:.1f}s total", flush=True)
    except Exception as exc:
        report.write_text(json.dumps({"settings": applied, "phases": records, "error": str(exc)}, indent=2))
        raise


if __name__ == "__main__":
    main()
