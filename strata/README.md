# Strata — local MoE engine

[Strata](https://github.com/Niko1221/Strata) runs large mixture-of-experts
models on a single consumer GPU by keeping the hot experts in VRAM and
streaming the rest from RAM (and, for Hy4-Flash, from the SSD). It serves an
OpenAI-compatible API (`/v1/chat/completions`, `/v1/models`) and an
Anthropic-compatible one (`/v1/messages`), plus a web chat and monitor.

It is **not** a replacement for the llama.cpp stack: `llama-cpp` serves the
Qwen3.8-27B dense/abliterated slots and the small MiMo `bolt` slot; Strata
serves the much larger Qwen3.8-Flash-Next class that does not fit the 16 GB
card any other way, plus Inkling-2-Mini's native `.strata` build.

## Shape: one more stack folder, one GPU tenant

`strata/compose.yml` is one stack folder with two services (the two tenants),
like `llama-cpp` and `comfyui`, with `restart: "no"`: the card is 16 GB and
only one tenant may hold it. Each is **cold-started on demand by litellm** and
stopped after `IDLE_TIMEOUT`, exactly like
`llama-server`/`llama-companion`/`llama-bolt`:

```
client → litellm :4000 → (start strata, wait health) → strata :8090 → engine
                        ↑ stops it again after 900 s without requests
```

The second tenant (`strata-inkling`, port 8091) uses the same path; the two
entries in `MODEL_CONTAINERS` stop each other.

Wiring, all in `litellm/`:

| What | Where |
| --- | --- |
| Route `qwen3.8-flash-next` → `http://<lan>:8090/v1` | `litellm/config.yaml` |
| Idle management of the `strata` container | `litellm/compose.yml` → `MODEL_CONTAINERS` |
| Stopped before ComfyUI starts | `litellm/compose.yml` → `GPU_CONTAINERS` (comfyui-proxy) |
| litellm allowed to start/stop it at all | `portainer/lifecycle/policy.example.json` → `gpu_containers` (live copy: `/opt/secrets/lifecycle-policy.json`) |
| Shared service key | `/opt/secrets/strata.env` (`API_KEY`), loaded by both |

The last two are the ones a rebuild forgets: the lifecycle gateway enforces its
own container allowlist, so a correct `MODEL_CONTAINERS` entry still 403s until
`strata` is in the policy — and a missing key file silently turns every metrics
probe into `unknown` (see "Verify on first boot").

## Second tenant: Inkling-2-Mini on the same stack

`strata-inkling` is the same image and the same shape as the other managed GPU
tenants: another tenant of the one card, cold-started by litellm on the first
request for its model and stopped when idle. It exists because
Inkling-2-Mini has **its own shape engine** (`strata-inkling`, downloaded on
first start) — not the Qwen engine this stack runs. The model is Thinking
Machines' 26.4B MoE looped transformer (128 experts, 8 active; core layers
6-17 run 3x).

The build is the native **SQ-15 (14.35 GB)**. Strata's catalog calls it the
16 GiB-card build — *"flagship quality (beats UD-Q4_K_XL) … fast 3x
loop-pinned hybrid on 12–16 GiB cards"*. SQ-20 (19.35 GB) is the ≥24 GiB
lossless build and cannot hold its experts here: at 128K it kept 6.36 GiB in
VRAM, ran the rest on the CPU, and decoded at 13.1 tok/s.

SQ-15's expert bodies total 12.28 GB (11.44 GiB), so **all of them fit on this
card**. The flags are GiB (the planner counts GB): pin
`--vram-experts-gb 11.46` so the engine can hold all 11.44 GiB of experts on
the card, and `--ram-experts-gb 0.82` as a fallback, not a placement — experts
that no longer fit (a larger context, a smaller VRAM cap) land in RAM instead
of the SSD. At 256K/fp8 the fallback is unused: measured **40.7 tok/s decode
and 1284 tok/s prefill** on a 2.4K prompt, with 15.57 GB of the 17.1 GB card in
use (~1.5 GB left for the KV cache to grow into). This is the maintainer's
recommended 16 GB RDNA4 setup from issue #7022 (56–57 tok/s on an RX 9070 XT;
the RX 9060 XT has half the CUs). The automatic planner under-places because
its non-expert reserve is sized for larger cards: it put 7.84 GiB on the card
and left ~3.6 GiB of experts in RAM, not the ~1–1.5 GiB of excess reserve the
maintainer plans to lower upstream. That placement also looked like "6 GB
free" on an idle card, but that space is not a fixed KV reservation on current
AMD builds: 0.2.21.1 changed the planner to keep only the first 32K of KV on
the card, and the engine grows the cache into free VRAM and spills the rest to
RAM when a conversation outgrows it. We run **256K (the trained window)** with
all experts resident: K/V stays on the card for roughly the first 30–50K
tokens, then spills. Every expert left in RAM costs about 0.16 ms/token, which
is why that placement measured only 24–27 tok/s. DSpark stays off: the probe
measured 0.92x at 128K (0.80x at 256K), and the maintainer's 16 GB recipe
disables it too.

Measurements at fp8, DSpark off:

| SQ-15 placement | Decode, 27-tok prompt | Decode, 2.4K prompt | Prefill, 2.4K prompt | VRAM |
| --- | ---: | ---: | ---: | ---: |
| 7.84 GiB experts (3.6 GiB in RAM, 128K) | 24.2 tok/s | 27.5 tok/s | 249 tok/s | 11.7 GB |
| **11.46 GiB cap (all experts on card, 0.82 GiB fallback unused, 256K)** | **42.6 tok/s** | **40.7 tok/s** | **1284 tok/s** | **15.6 GB** |

Long-context cost once K/V spills (same hidden-code prompt, reasoning off):

| Prompt | Prefill | Decode | Retrieval |
| --- | ---: | ---: | --- |
| 2.4K | 1284 tok/s | 40.7 tok/s | — |
| 50K | 995 tok/s | 31.7 tok/s | correct |
| 120K | 620 tok/s | 16.7 tok/s | correct |

The first 50K run used a 32-token budget and spent it all thinking; a client
asking long-context questions should raise `max_tokens` or set
`reasoning_effort`.

The largest prompt verified end to end is 120K. The 256K setting is the
model's trained window, not tested headroom: a later run on the same
configuration died with `[ink-hip] alloc_gpu_buffer: out of memory` and the
engine restarted. A fresh short start is safe — an 18-token request used
14.94 GB of the 17.10 GB card, left 2.15 GB free, and spilled no KV — but a
long prompt after the card is already full can still OOM. If a run must not
fail, use 128K or lower.

**Why long context is slower than on Qwen3.8-Flash-Next.** Not engine and not
size: Inkling's 48 layer applications include eight **dense global** attention
passes (the rest use the 1024-token SWA ring), so every decoded token reads the
whole global K/V — 16 KiB/token at fp8, ~1.9 GB at 120K. Once that no longer
fits beside the experts it lives in host pinned memory, and decode becomes a
PCIe read: 1.9 GB/token x 16.7 tok/s ≈ 32 GB/s at 120K, 0.8 GB x 31.7 ≈
25 GB/s at 50K. Qwen3.8-Flash-Next is 125B/6B active but three of every four
layers are Gated DeltaNet (fixed state) and the fourth is Qwen Sparse Attention
with a 512-block/2048-token budget, so its per-token attention is bounded and
its KV is 0.84 GiB at 128K; decode stays 48–49 tok/s even at 135K.

Three levers that look tempting and are not (measured 2026-11-26):

| Attempt | Result |
| --- | --- |
| Trade experts for K/V room (`10.9 / 1.4`) | 120K decode 16.7 → **20.4**, but prefill 1220 → **351** at 2.6K and 995 → 332 at 50K; 120K wall 194 s → **415 s**. RAM experts are computed on the CPU, and prefill touches nearly every expert while decode routes 8 of 128. |
| `--kv k8v4` (25% smaller K/V) | Not available on HIP: the launcher clamps it to fp8 (`INKLING_KV_MODES_SAFE`, issue #6681). CUDA only. |
| DSpark drafting (`--draft dspark` / `DRAFTING=mtp`) | Slower and it takes ~1 GiB: prose 42.5 → **34.4**, code 43.2 → 42.0, JSON 43.6 → **33.9**, 2.6K 40.5 → **33.9**; accepted 22–37% of drafts; prefill 1220 → 921 and the chunk cap drops 512 → 256. Off stays. |

So `11.46 / 0.82` at 256K/fp8 is the balance: best prefill, honest speed to
~50K, and graceful degradation after that. Use Qwen3.8-Flash-Next for long
documents.

Two traps in those numbers. **Short prompts look slow:** a 27-token prompt is
mostly graph setup, so prefill must be measured on a long prompt. **The
launcher plans from polluted memory:** it computes placement while its own
probe engine already holds the ~3.4 GiB dense trunk, and its reserve is larger
than a 16 GB card needs, so it under-allocates experts (2.47–7.84 GiB in our
runs). Pin the placement in `data/install/strata-inkling-2-mini-sq-15.json` and
`data/state/strata/launch/inkling.json`; the launcher reuses those args until a
setting changes, and any change to model/context/KV triggers a fresh (bad) plan.
Both files are root-owned and repeat the args under more than one key, so edit
them inside the container, then recreate. The `docker exec` needs the tenant
running; start it first if it is stopped (with `strata` stopped too):

```sh
cd /opt/stacks/strata
docker exec -i strata-inkling /opt/strata/terminal/python/bin/python3 - <<'PY'
import json
for path in ("/data/install/strata-inkling-2-mini-sq-15.json",
             "/data/state/strata/launch/inkling.json"):
    d = json.load(open(path))
    for key in ("args", "args_written", "args_derived"):
        a = d.get(key)
        if not a:
            continue
        for flag, value in (("--vram-experts-gb", "11.46"),
                            ("--ram-experts-gb", "0.82")):
            if flag in a:
                a[a.index(flag) + 1] = value
            else:
                a += [flag, value]
    with open(path, "w") as f:
        json.dump(d, f, indent=1)
PY
# only while `strata` is stopped (both tenants share /data):
docker compose up -d --force-recreate strata-inkling
```

The first start writes the planner's values; the pin above is only needed when
those values under-place the experts. Re-apply it after a model, context or KV
change has forced a fresh plan.

So SQ-15 is the better overall answer here: 40.7 tok/s decode, 1284 tok/s
prefill on a 2.4K prompt, and the trained 256K window (verified to 120K; see
the OOM note above). The SQ-20 pack (19.35 GB) is only worth running if
bit-exact lossless matters more than 2x speed, and it wants a 24 GiB card.
The 0.64 GB Vision shard is separate from the 14.35 GB LM file.

| What | Value |
| --- | --- |
| Service / container | `strata-inkling`, port `8091` |
| Settings | `strata/.env-inkling` (gitignored; `.env-inkling.example` is the mirror) |
| Build | `SQ-15` (14.35 GB LM + 0.64 GB vision), all 11.44 GiB of experts on the card: `--vram-experts-gb 11.46` GiB, `--ram-experts-gb 0.82` GiB fallback |
| `PRESET` | `inkling` — both tenants share `/data`, so the preset name keeps their presets, launch files and engine logs apart |
| Context | `262144` (256K, the trained window; fp8 K/V) |
| Route | `inkling-2-mini` → `http://<lan>:8091/v1`; OpenCode lists only this one |
| Vision | `VISION=on`, CPU tower; first image after a start ~148 s (tower load), then ~2 s per image |
| Idle management | its own `MODEL_CONTAINERS` entry, `boot_timeout` 1200 (first start downloads the pack *and* the shape engine) |

The image is pinned at `STRATA_IMAGE_TAG=0.2.22-rocm-gfx120x`. That release
carries Control fixes only for this host (including the 0.2.21.1 change that
keeps just the first 32K of K/V on the card): its Inkling engine changes are
CUDA-only, and the AMD Inkling engine stays 0.1.67.

Two more things to know. **Context:** 400K is accepted but the planner warns it
is beyond the trained window — `400K is more than the model was trained on
(256K). Setup adds rope scaling for it (experimental); answers may get less
reliable.` **Never start both services by hand:** the two tenants share one
data volume and Strata runs one model from it at a time; litellm's mutual
exclusion is what keeps them apart.

Three details make the idle manager work with Strata, because Strata is not
llama.cpp:

1. **Metrics dialect.** Strata publishes vLLM's metric names
   (`vllm:num_requests_running`, `vllm:num_requests_waiting`) instead of
   llama.cpp's `requests_processing`/`requests_deferred`. `IDLE_GAUGE_FAMILIES`
   in `litellm/custom_callbacks.py` accepts both; llama.cpp's gauges win when a
   backend offers both, so the existing slots are unchanged.
2. **The text body.** `/metrics` answers JSON to a plain request (the web
   monitor reads it) and Prometheus text only for `?format=prometheus` — hence
   the query string in the `metrics` URL.
3. **The key.** `/metrics` is not anonymous; the probe sends the service key as
   a bearer token (`metrics_key_env: API_KEY`).

## Hardware and model

Host after the RAM upgrade: 64 GB, Ryzen 5 5600XT, **Radeon RX 9060 XT 16 GB**
(RDNA 4, `gfx1200`) → image flavor `rocm-gfx120x`.

Configured pack: **Qwen3.8-Flash-Next IQ3_S** (ISTA-DASLab), 54.8 GB of weights.

| Size | Weights | Note |
| --- | ---: | --- |
| Q2_0 | 37.6 GB | fastest, "good" |
| IQ2_XS | 39.2 GB | fast, "better" |
| IQ3_XXS | 47.0 GB | slower, "great" |
| **IQ3_S** | **54.8 GB** | slowest, "best"; matches the full model on the published tests |

Upstream says IQ3_S fits 64 GB "with little else open". This host idles at
~10 GB used (immich, jellyfin, paperless, open-webui, litellm, engram…), so the
fit is tight by design. If it thrashes:

- drop `CONTEXT` (the KV cache competes with the experts for VRAM),
- stop a heavy service while using the model, or
- switch `MODEL` to `IQ3_XXS` (47 GB) — a new download, which is why the size
  is worth deciding once.

Switching family (`FAMILY=hy4-flash` with `MODEL=SQ-64`) is possible at any
time; that pack is 62 GB plus a 67.6 GB Engram shard streamed from the SSD.

## First start

```sh
cd /opt/stacks/strata
cp .env.example .env                      # fresh checkout: once, then edit
cp .env-inkling.example .env-inkling      # once; compose parses both services
docker compose up -d strata               # only the Qwen tenant
docker logs -f strata                     # first start: the download
docker exec -it strata strata monitor     # live monitor (q to leave)
```

Do not use a bare `docker compose up -d` here: it starts both tenants, and
both share `/data`. Litellm normally cold-starts whichever model is requested,
so after the first download neither needs to stay up.

Expect a long first start: the first run downloads the pack — for IQ3_S that
is two shards, 54.8 GB + 28.8 GB = **83.6 GB** — plus the ~6 GB MTP drafter,
into `./data`, and then prepares the pack (~1.5 GB). Later starts re-read the
prepared pack from the SSD — still tens of seconds to minutes, which is why
`boot_timeout` is 600 in `MODEL_CONTAINERS`.

### Inkling tenant first start

The second tenant has its own settings file and its own service. Its first
start downloads the 14.35 GB SQ-15 pack, the 0.64 GB vision shard, and the
`strata-inkling` shape engine; `boot_timeout` is 1200 for that reason. Run it
only while `strata` is stopped, then stop it again and let litellm cold-start
it on demand:

```sh
cd /opt/stacks/strata
cp .env-inkling.example .env-inkling      # fresh checkout: once, then edit
docker compose stop strata                # they share /data
docker compose up -d strata-inkling
docker logs -f strata-inkling             # first start: the downloads
docker compose stop strata-inkling
```

After changing `.env-inkling`, recreate the tenant with
`docker compose up -d --force-recreate strata-inkling` (again, only while
`strata` is stopped). The entrypoint rewrites the launch preset on every
start; the expert caps live in the two JSON files described above, so re-apply
them if a model/context/KV change forced a fresh plan.

To use files downloaded elsewhere instead (no download, no re-pack):

```sh
# one model's files, directly in ./models/IQ3_S (not a folder of folders)
hf download ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF --include "IQ3_S/*" --local-dir /opt/stacks/strata/models
# then set MODEL_PATH=/models/IQ3_S in .env and:
docker compose up -d strata
```

The pack layout is one folder per size (`Q2_0/`, `IQ2_XS/`, `IQ3_XXS/`,
`IQ3_S/`), so `MODEL_PATH` names the size folder, not the repo root: shard 1
carries the weights (the experts go to RAM, the rest to VRAM) and shard 2 is
the 28.8 GB lookup table that Strata reads from the SSD at request time — which
is why this wants the NVMe, not a SATA disk.

Upstream publishes the packs on Hugging Face
(`ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF` for these,
`strata-engine/Hy4-Flash-Strata` for the native Hy4 packs).

## Verify on first boot

The first check decides whether idle management works at all; the rest are
cheap confirmations. All of them use the key:

```sh
KEY=$(docker exec strata strata launch --show-key --preset docker)
```

1. **Idle-stop wiring — the one that matters.** The idle manager probes the
   backend *before every stop* and refuses to stop unless the probe answers
   `idle` (`custom_callbacks.py`, `_idle_container`). So a metrics endpoint it
   cannot read means strata is never stopped and 55 GB stay resident — the
   failure is silent apart from one log line per tick. Rehearse the exact
   request it makes, with the model idle:

   ```sh
   curl -s -H "Authorization: Bearer $KEY" 'http://192.168.0.46:8090/metrics?format=prometheus' | grep -E 'vllm:num_requests_(running|waiting)'
   ```

   Both gauges must appear and read `0`. A `401`, a JSON body, or no lines at
   all kills the stop gate; the likely causes are the key (a later `env_file`
   in litellm shadowing `API_KEY`), the missing `?format=prometheus`, a renamed
   gauge upstream, or an unreachable address. Afterwards the watcher names its
   verdict — `docker logs litellm 2>&1 | grep 'strata backend probe'` must say
   `idle`, never `unknown; not stopping`.
   *Measured 2026-11-25:* both gauges `0` (with a `model_name` label — the
   parser handles labels), `401` without the key, JSON without the query
   string, and the watcher logged `registered strata token=…` /
   `released … reason=post_call_success` for a real request.
2. **Model id.** `curl -s -H "Authorization: Bearer $KEY" http://127.0.0.1:8090/v1/models | jq -r '.data[].id'`
   lists the family **plus the size**: measured `qwen3.8-flash-next-iq3_s` on
   this host, which is why `litellm/config.yaml` names that id upstream and
   `MODEL_CONTAINERS` carries it under `aliases` (the idle timer resolves the
   upstream name from the logs). Strata answers to any name unless started with
   `--strict-model`, so a mismatch would not 400 — it would only make responses
   and logs disagree.
3. **Readiness — measured 2026-11-25, no action needed.** A cold start is safe
   by construction, not by luck: Strata's `Service.__init__` blocks until the
   engine reports READY and only then calls `serve()` to bind the port
   (`terminal/serve/server.py`; `--lazy` is off by default). Measured: a
   cold start triggered through litellm answered in **35 s** (`http=200`), so
   `wait_health` waited instead of admitting the request early. After an engine
   *restart* the server is already up while the engine reloads: `/health` then
   answers `200` with `"loaded": false` and requests get `503 starting` — a
   surfaced error, not a hang.
4. **Stop grace — measured 2026-11-25, the 10 s default does not bite.**
   `docker inspect strata --format '{{.HostConfig.StopTimeout}}'` is `null`:
   Compose's `stop_grace_period: 2m` is not stored on the container, so the idle
   stop (gateway → Portainer → Docker `POST /stop` without `t`) gives the engine
   10 s. Measured with the same call: **stopped in 2 s with exit 0**, logged
   "Stop requested: shutting down", and released all 51 GiB. A clean unload fits
   inside the default, so nothing needs changing — re-check if a future release
   ever takes longer than 10 s to unload.

### The Inkling tenant

The same four checks apply to `strata-inkling`; substitute the container,
preset and port:

```sh
KEY=$(docker exec strata-inkling strata launch --show-key --preset inkling)
curl -s -H "Authorization: Bearer $KEY" 'http://192.168.0.46:8091/metrics?format=prometheus' | grep -E 'vllm:num_requests_(running|waiting)'
curl -s -H "Authorization: Bearer $KEY" http://127.0.0.1:8091/v1/models | jq -r '.data[].id'
```

Measured `inkling-2-mini-sq-15`; that is the upstream name in
`litellm/config.yaml` and the `MODEL_CONTAINERS` alias. A cold start is slower
than the Qwen pack's because the 14.35 GB pack and the shape engine are read
from the SSD, which is why `boot_timeout` is 1200. The stop gate is the same
vLLM gauges; the watcher's backend-probe line must say `idle`, never
`unknown; not stopping`. The stop-grace numbers above are from `strata`;
`strata-inkling` has the same 2m compose grace, but its 10 s default unload was
not measured.

## Removing it again

Remove the Qwen tenant:

```sh
cd /opt/stacks/strata && docker compose stop strata && docker compose rm -f strata
# then drop the five pieces in litellm/: the qwen3.8-flash-next entry in
# config.yaml, the entry in MODEL_CONTAINERS, STRATA_API_BASE, the
# /opt/secrets/strata.env env_file line, and `strata` from the comfyui-proxy
# GPU_CONTAINERS list; restart litellm.
```

Remove the Inkling tenant:

```sh
cd /opt/stacks/strata && docker compose stop strata-inkling && docker compose rm -f strata-inkling
# then drop: the inkling-2-mini entry in litellm/config.yaml, its
# MODEL_CONTAINERS entry, STRATA_INKLING_API_BASE, strata-inkling from
# GPU_CONTAINERS (comfyui-proxy), strata-inkling from
# scripts/server-health.sh ALLOW_STOPPED, `strata-inkling` from the lifecycle
# policy's gpu_containers, and .env-inkling; restart litellm. The 14.35 GB
# pack, 0.64 GB vision shard and the packs/ tokenizer dirs live under
# data/Strata-data/ and can be deleted.
```

The `/opt/secrets/lifecycle-policy.json` grant and the secret file itself
outlive this; remove the tenant names from `gpu_containers` (and recreate the
gateway) and delete the secret if the stack is gone for good.

## Pictures

`VISION=on` fetches Strata's picture reader (0.9 GB) and loads it as a CPU
tower: `strata-vision: own tower (qwen3vl_merger, 27 layers, width 1152) loaded
in 0.7 s, on the CPU, 6 threads`. It uses RAM, not VRAM.

Measured on a generated 720x400 test card through litellm: all four text lines
read exactly and both shapes named correctly, 5.5 s round trip of which ~2 s is
the CPU encode. The same request logged `prompt 384 tokens … read in 1355 ms
(283.5 tok/s)` — the image's tokens arrive as prompt tokens and prefill faster
than short text prompts because the batch is bigger — with decode unchanged at
48.4 tok/s.

`gpu` does not fit this card: with the model loaded there are under 900 MiB of
VRAM spare. Open WebUI needs the model flagged vision-capable in its own model
settings before it offers an upload button; the API path (`image_url` parts
through litellm) needs nothing extra.

The Inkling tenant also runs `VISION=on` (its own `Inkling-2-Mini-Vision.strata`
tower, engine arg `--vision`), measured 2026-11-26 on 512x512 cards through
litellm: the **first** image took 148.6 s because that request loads the CPU
tower, the **next** image read correctly in **2.1 s**. Decode is unaffected
(42.7 tok/s). So the first image after a tenant start is a one-off load, not a
per-image cost. One quirk of SQ-15: short answers often come back doubled
(`VISION OK 4471` twice) — harmless, but a client that needs exact text should
trim or ask for a single phrase.

## Context and caching

`CONTEXT=196608` on this card, measured 2026-11-25 up from 64k: no VRAM cost
(835-884 MiB free at every step, because only `--kv-resident 32768` sits in
VRAM and the rest streams) and no decode cost (48.0-49.0 tok/s). The raised
window costs pinned RAM instead, since the whole K/V is kept there: 0.84 GiB at
128k, 1.27 GiB at 192k. Prefill grows with the batch — 707 tok/s at 3k, 983 at
30k, 924 at 48k, 985 on a 135107-token prompt that took 137 s end to end
(measured past the old 128k ceiling, so the window is real).

`--expert-cache` stays on `auto` on purpose. Pinning it to 5400 slots clamped
to 5284 slots / 10.05 GiB and left 203 MiB of the card free, at which point the
engine printed `decode can be several times slower with the card this full`.
Auto keeps ~835 MiB back in three named pieces the launcher logs: 700 MiB
reserve, +380 for the draft head, +256 for what is allocated after the cache.
That is the working margin, not waste — and the reason `STRATA_SET` must not
pin `expert_cache`; its two entries only size the conversation cache.

The KV format is `KV=k8v4` (INT8 K, so exact attention scores, plus a rotated
Q4_0 V, 816 B/cell). Measured 2026-11-25 at 192k against `q4_0` (528 B/cell):
same speed (49.2 vs 48.0 tok/s decode, 925 vs 931 tok/s on a 12.1k prompt) and
same VRAM, for 0.52 GiB more pinned RAM (1.79 vs 1.27 GiB for the whole K/V).

Two different caches, easy to confuse in the log:

- **Prefix reuse is on** (`--prompt-cache-spread`). A second turn on the same
  conversation reuses the parked KV: measured `3123 tokens = 3069 reused + 54
  read in 791 ms` for a turn that cost 4.3 s cold. This is the normal case.
- **The conversation cache is on** since 2026-11-25, but it needs two settings,
  not one — a budget **and** a floor. It ships as
  `STRATA_SET="conversation_cache_mib=2048 conversation_cache_min_free_mib=512"`;
  4096/1024 was the first pair tried, and the measurements below are what walked
  it down. Lowering only the floor left the log saying `conversation
  cache off: not enough free RAM to park conversations`; the auto-sized budget
  never cleared the engine's own floor.
  Turning it on also brings the engine's overflow tier, `conversation cache: SSD
  tier in /data/Strata-data/conversation-spill/<id>, up to 32768 MiB` — no flag
  sizes or disables it, and it writes only when an eviction actually sticks.
  The budget decides the **largest** conversation it can park, not how many. A
  snapshot costs ~20-22 MiB per 1k tokens (12k → 356 MiB, 30k → 664 MiB, 48k →
  971 MiB), so the 512 MiB first try could never hold a 30k conversation and
  4096 can. Admission needs snapshot + floor under MemAvailable and evicts
  parked snapshots when that covers the shortfall: `evicted 3 parked
  conversations to admit this snapshot (664 MiB plus 1024 MiB floor)`, then
  `parked 30170 tokens in 968.9 ms`, repeat prompt **1.29 s against 32.0 s
  cold** (`restored 30102 tokens (checkpoint) in 56.3 ms`).
  Where the floor puts the ceiling: admission is snapshot + floor under
  MemAvailable, so the floor decides what can ever be parked. At 1024 a 48k
  conversation was refused (`need 971 MiB plus 1024 MiB floor, 965 MiB
  available ... short by 1029 MiB`); at **512** it parks — `parked 48170 tokens
  in 290.1 ms; parked=1 bytes=1018591768` (971 MiB), repeat in 1.45 s against
  84.6 s cold. Caveat: that snapshot did not survive the next request
  (`parked=0` right after), so the fast repeat came from the VRAM checkpoint ring
  (`restored 48102 tokens (checkpoint) in 77.3 ms`) — on a tight host, parked
  snapshots are the first thing given back. A temporary floor of 128 was backed
  out as too aggressive.
  A real 157–160k session needs **~2.5 GiB** per snapshot (`need 2485 MiB plus
  1024 MiB floor, 986 MiB available ... short by 2523 MiB`), so no budget size
  saves it — the snapshot already fits a 4096 budget and admission is what
  refuses it. At that size the prefix checkpoints carry the session: the same
  log shows turns reusing in 0.31–1.33 s between the occasional 163 s cold
  prefill. `conversation_cache_swap_block_mib` is not a way around this; it is
  only documented as `host K/V exchange block, 0..256 MiB (default 0: off)` and
  a 48k probe with 256 parked nothing, so it was reverted.
  The engine also sets its own `--conversation-cache-min-tokens 1024`, so tiny
  side requests are skipped (`skip parking (53 tokens, below the 1024 minimum)`).

## Notes

- The container runs as **root with `no-new-privileges:true`**. uid 1000
  (`user: "1000:1000"`, the image's own `ubuntu` user) was tried on 2026-11-25
  and reverted the same day: it served fine, but every start printed `cannot
  create /run/strata-phase: Permission denied`, a future `TUNE=quick` would fail
  writing the dflash2-tuned artifacts into the read-only image layer, and
  switching back is not free — the launcher refuses `state/strata/runtime`
  (`runtime directory must be owner-only (mode 0700)`, exit 2) because that
  directory is owned by 1000; `rm -rf data/state/strata/runtime` and let root
  recreate it. `data/` therefore still carries 1000 ownership on the host, which
  root writes fine, so no chown back is required.
- No memory limit on the container on purpose: Strata plans from the whole
  computer's RAM. A limit turns a tight fit into an OOM kill.
- `ulimits.memlock: -1` lets it pin the expert weights in RAM; without it the
  kernel swaps them out and throughput collapses.
- `stop_grace_period: 2m` gives a 55 GB model time to unload (Docker's 10 s
  default would kill it with exit 137).
- The image ships its own healthcheck (`GET /v1/models` with the key); do not
  add a second one.
- `./data` and `./models` are gitignored. The key is defined in
  `/opt/secrets/strata.env`; the engine also keeps its own copy at
  `data/state/api-key` (mode 0600) so a restart needs no injection. That copy is
  gitignored but it does ride along in the NAS backup, which excludes only
  `models/` and `data/Strata-data/`.
- Every setting is declarative: the entrypoint turns `.env` into `strata launch`
  flags and re-saves the launch preset (`data/config/strata/launch-presets/
  docker.json`) on every start, so a fresh
  `docker compose up -d --force-recreate` reproduces the state with nothing to
  change inside the container — that is how 192k and `k8v4` were applied. The
  one exception is an engine setting passed through `STRATA_SET`: it lands in
  the preset's `extra` object and stays there after the variable is emptied, so
  clear it in that file (root-owned; use `docker exec ... sed`).
- Qwen3.8-Flash-Next is also the coding CLIs' backend, both through litellm so
  idle management still applies: `qwen3.8-flash-next` in `vibe/config.toml`
  (provider `llamacpp`, 196608 window, images on) and
  `llama-cpp/qwen3.8-flash-next` in `opencode.json`. Verified 2026-11-25 by a
  real turn each way (`vibe -p ... --model qwen3.8-flash-next`, `opencode run
  -m llama-cpp/qwen3.8-flash-next`). Images work, at the CPU reader's ~5.5 s
  each. Inkling is `llama-cpp/inkling-2-mini` in `opencode.json`
  (262144 window, variants none..max); it is not in Vibe.
- Qwen3.8-Flash-Next's reasoning levels work, and they are the four that
  engine accepts — `none/off`, `low`, `medium`, `high|max|xhigh` (both `high`
  and `xhigh` land on `xhigh`; anything else is a 400 naming the four).
  Measured on one arithmetic prompt: `none` = 0 reasoning tokens, `low` = 225,
  `xhigh` = 158. Vibe selects them with `/thinking` (default `medium`),
  OpenCode with the model's variants. Inkling's engine takes
  `none..max` levels; its OpenCode entry exposes all six, default `high`.
- **Firewall:** neither 8090 (`strata`) nor 8091 (`strata-inkling`) is in the
  LAN allowlist of `firewall/firewall-setup.py` (`TCP_PORTS`), so only this
  host and containers reach them today — enough for litellm and Open WebUI via
  litellm. To open a Strata web UI / monitor to other devices, add that port to
  `TCP_PORTS` there and re-run the firewall deployment (a deliberate change to
  host policy, not part of this stack). Note what that publishes: this stack
  has no TLS, so the service key travels in cleartext on every request and on
  every metrics probe, and the keyed chat UI becomes reachable by every LAN
  device. Keep the key strong and treat the LAN as trusted, or front it with
  the tunnel.
