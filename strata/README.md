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
| Strata stopped before ComfyUI starts | `litellm/compose.yml` → `GPU_CONTAINERS` (comfyui-proxy) |
| Shared service key | `/opt/secrets/strata.env` (`API_KEY`), loaded by both |

Three details make the idle manager work with Strata, because Strata is not
llama.cpp:

1. **Metrics dialect.** Strata publishes vLLM's metric names
   (`vllm:num_requests_running`, `vllm:num_requests_waiting`) instead of
   llama.cpp's `requests_processing`/`requests_deferred`. `IdleGaugeFamilies`
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

Ratings: the first start downloads 66-76 GB (plus the ~6 GB MTP drafter) into
`./data`, then prepares the pack. Later starts re-read the prepared pack from
the SSD — still tens of seconds to minutes, which is why `boot_timeout` is 600
in `MODEL_CONTAINERS`.

To use files downloaded elsewhere instead (no download, no re-pack):

```sh
# files must sit directly in ./models (not a folder of folders)
docker compose stop strata
# set MODEL_PATH=/models in .env, then:
docker compose up -d
```

Upstream publishes the packs on Hugging Face
(`strata-engine/Hy4-Flash-Strata`, `ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF`
→ `hf download <repo> --local-dir models`).

## Verify on first boot

The idle manager needs two things that only a live container can confirm:

1. **Readiness.** `GET /health` answers `200` with `"loaded": false` while the
   model is still loading. `wait_health` treats two consecutive `200`s as
   ready, so if Strata's HTTP server binds *before* the load finishes, litellm
   admits a request into a loading model. Check with:

   ```sh
   docker exec strata strata launch --show-key --preset docker   # the key
   curl -s http://127.0.0.1:8090/health | jq .loaded            # during load
   ```

   If it reports `false` while the port already answers, the readiness probe
   needs a body check (a small, isolated change to
   `gpu_containers.wait_health`). Otherwise nothing to do.
2. **Model id.** `curl -s -H "Authorization: Bearer $KEY" \
   http://127.0.0.1:8090/v1/models | jq -r '.data[].id'` should list
   `qwen3.8-flash-next`. Strata answers to any name unless started with
   `--strict-model`, so a different id would not 400 — but update
   `litellm/config.yaml` to the reported id if it differs.

## Removing it again

```sh
cd /opt/stacks/strata && docker compose down      # keeps ./data
# then drop the four pieces in litellm/: the qwen3.8-flash-next entry in
# config.yaml, the entry in MODEL_CONTAINERS, STRATA_API_BASE, and `strata`
# from the comfyui-proxy GPU_CONTAINERS list; restart litellm.
```

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
- **Firewall:** port 8090 is not in the LAN allowlist of
  `firewall/firewall-setup.py` (`TCP_PORTS`), so only this host and containers
  reach it today — enough for litellm and Open WebUI via litellm. To open the
  Strata web UI / monitor to other devices, add 8090 to `TCP_PORTS` there and
  re-run the firewall deployment (a deliberate change to host policy, not part
  of this stack).
