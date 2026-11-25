# Strata — local MoE engine

[Strata](https://github.com/Niko1221/Strata) runs large mixture-of-experts
models on a single consumer GPU by keeping the hot experts in VRAM and
streaming the rest from RAM (and, for Hy4-Flash, from the SSD). It serves an
OpenAI-compatible API (`/v1/chat/completions`, `/v1/models`) and an
Anthropic-compatible one (`/v1/messages`), plus a web chat and monitor.

It is **not** a replacement for the llama.cpp stack: `llama-cpp` serves the
Qwen3.8-27B dense/abliterated slots and Inkling-2-Mini at higher speed; Strata
serves the much larger Qwen3.8-Flash-Next class that does not fit the 16 GB
card any other way.

## Shape: one more stack folder, one GPU tenant

`strata/compose.yml` is a single service, like `llama-cpp` and `comfyui`, with
`restart: "no"`: the card is 16 GB and only one tenant may hold it. It is
**cold-started on demand by litellm** and stopped after `IDLE_TIMEOUT`, exactly
like `llama-server`/`llama-companion`/`llama-bolt`:

```
client → litellm :4000 → (start strata, wait health) → strata :8090 → engine
                        ↑ stops it again after 900 s without requests
```

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
docker compose up -d
docker logs -f strata                     # first start: the download
docker exec -it strata strata monitor     # live monitor (q to leave)
```

Expect a long first start: the first run downloads the pack — for IQ3_S that
is two shards, 54.8 GB + 28.8 GB = **83.6 GB** — plus the ~6 GB MTP drafter,
into `./data`, and then prepares the pack (~1.5 GB). Later starts re-read the
prepared pack from the SSD — still tens of seconds to minutes, which is why
`boot_timeout` is 600 in `MODEL_CONTAINERS`.

To use files downloaded elsewhere instead (no download, no re-pack):

```sh
# one model's files, directly in ./models/IQ3_S (not a folder of folders)
hf download ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF --include "IQ3_S/*" --local-dir /opt/stacks/strata/models
# then set MODEL_PATH=/models/IQ3_S in .env and:
docker compose up -d
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

## Removing it again

```sh
cd /opt/stacks/strata && docker compose down      # keeps ./data
# then drop the five pieces in litellm/: the qwen3.8-flash-next entry in
# config.yaml, the entry in MODEL_CONTAINERS, STRATA_API_BASE, the
# /opt/secrets/strata.env env_file line, and `strata` from the comfyui-proxy
# GPU_CONTAINERS list; restart litellm.
```

The `/opt/secrets/lifecycle-policy.json` grant and the secret file itself
outlive this; remove `strata` from `gpu_containers` (and recreate the gateway)
and delete the secret if the stack is gone for good.

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
That is the working margin, not waste, and `STRATA_SET` is empty here.

The KV format is `KV=k8v4` (INT8 K, so exact attention scores, plus a rotated
Q4_0 V, 816 B/cell). Measured 2026-11-25 at 192k against `q4_0` (528 B/cell):
same speed (49.2 vs 48.0 tok/s decode, 925 vs 931 tok/s on a 12.1k prompt) and
same VRAM, for 0.52 GiB more pinned RAM (1.79 vs 1.27 GiB for the whole K/V).

Two different caches, easy to confuse in the log:

- **Prefix reuse is on** (`--prompt-cache-spread`). A second turn on the same
  conversation reuses the parked KV: measured `3123 tokens = 3069 reused + 54
  read in 791 ms` for a turn that cost 4.3 s cold. This is the normal case.
- **The conversation cache is on** since 2026-11-25, but it needed two settings,
  not one: `conversation_cache_mib=4096` **and** `conversation_cache_min_free_mib=1024`
  via `STRATA_SET`. Lowering only the floor left the log saying `conversation
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
  Where it stops: a 48k conversation was refused — `need 971 MiB plus 1024 MiB
  floor, 965 MiB available ... short by 1029 MiB, more than the 1 parked hold
  (664 MiB), so no eviction would help` — and repeated at 28.9 s against 29.1 s
  cold, no gain. Freeing ~1 GiB (`VISION=off` is 0.9) or dropping the floor to
  768 is what brings 48k into reach; the budget alone cannot.
  A real 157–160k session needs **~2.5 GiB** per snapshot (`need 2485 MiB plus
  1024 MiB floor, 986 MiB available ... short by 2523 MiB`), so the 8192 budget
  changes nothing there — 4096 already covered the snapshot and admission
  refused it. At that size the prefix checkpoints carry the session: the same
  log shows turns reusing in 0.31–1.33 s between the occasional 163 s cold
  prefill. `conversation_cache_swap_block_mib` is not a way around this; it is
  only documented as `host K/V exchange block, 0..256 MiB (default 0: off)` and
  a 48k probe with 256 parked nothing, so it was reverted.
  The engine also sets its own `--conversation-cache-min-tokens 1024`, so tiny
  side requests are skipped (`skip parking (53 tokens, below the 1024 minimum)`).

## Notes

- No memory limit on the container on purpose: Strata plans from the whole
  computer's RAM. A limit turns a tight fit into an OOM kill.
- `ulimits.memlock: -1` lets it pin the expert weights in RAM; without it the
  kernel swaps them out and throughput collapses.
- `stop_grace_period: 2m` gives a 55 GB model time to unload (Docker's 10 s
  default would kill it with exit 137).
- The image ships its own healthcheck (`GET /v1/models` with the key); do not
  add a second one.
- `./data` and `./models` are gitignored. The key lives in
  `/opt/secrets/strata.env`, never in this folder.
- Every setting is declarative: the entrypoint turns `.env` into `strata launch`
  flags and re-saves the launch preset (`data/config/strata/launch-presets/
  docker.json`, `extra: {}` here) on every start, so a fresh
  `docker compose up -d --force-recreate` reproduces the state with nothing to
  change inside the container — that is how 192k and `k8v4` were applied. The
  one exception is an engine setting passed through `STRATA_SET`: it lands in
  the preset's `extra` object and stays there after the variable is emptied, so
  clear it in that file (root-owned; use `docker exec ... sed`).
- The model is also the coding CLIs' backend, both through litellm so idle
  management still applies: `qwen3.8-flash-next` in `vibe/config.toml` (provider
  `llamacpp`, 196608 window, images on) and `llama-cpp/qwen3.8-flash-next` in
  `opencode.json`. Verified 2026-11-25 by a real turn each way
  (`vibe -p ... --model qwen3.8-flash-next`, `opencode run -m
  llama-cpp/qwen3.8-flash-next`). Images work, at the CPU reader's ~5.5 s each.
- Reasoning levels work, and they are the four the engine accepts — `none/off`,
  `low`, `medium`, `high|max|xhigh` (both `high` and `xhigh` land on `xhigh`;
  anything else is a 400 naming the four). Measured on one arithmetic prompt:
  `none` = 0 reasoning tokens, `low` = 225, `xhigh` = 158. Vibe selects them
  with `/thinking` (default `medium`), OpenCode with the model's variants.
- **Firewall:** port 8090 is not in the LAN allowlist of
  `firewall/firewall-setup.py` (`TCP_PORTS`), so only this host and containers
  reach it today — enough for litellm and Open WebUI via litellm. To open the
  Strata web UI / monitor to other devices, add 8090 to `TCP_PORTS` there and
  re-run the firewall deployment (a deliberate change to host policy, not part
  of this stack). Note what that publishes: this stack has no TLS, so the
  service key travels in cleartext on every request and on every metrics probe,
  and the keyed chat UI becomes reachable by every LAN device. Keep the key
  strong and treat the LAN as trusted, or front it with the tunnel.
