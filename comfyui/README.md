# ComfyUI (RDNA 4 / RX 9060 XT)

Local image generation on the pop os server (`<your-LAN-IP>`), using the AMD RX 9060 XT 16 GB through ROCm.

This stack deliberately builds a local ComfyUI image from a **pinned AMD ROCm/PyTorch base**. It does not use the generic `rocm/comfyui` image and does not rely on a `latest` tag or changes made manually inside a running container.

## Stack

- GPU: AMD Radeon RX 9060 XT, 16 GB (RDNA 4 / gfx1200)
- Base image: `rocm/pytorch:rocm7.2.1_ubuntu24.04_py3.12_pytorch_release_2.9.1`
- Runtime: Docker Compose with `/dev/kfd` and `/dev/dri` passed through
- UI: ComfyUI on port `8188`
- Clients: lifecycle proxy on port `8189` (Open WebUI and MCP)
- Persistent data: host bind mounts under `/opt/stacks/comfyui/`

`HSA_OVERRIDE_GFX_VERSION` is intentionally **not** set. The RX 9060 XT is native gfx1200; use ROCm's normal hardware detection first rather than forcing a different architecture.

## Lifecycle

`comfyui` runs with `restart: "no"`. It is normally started on demand and
stopped when idle by **comfyui-proxy** in the litellm stack, which fronts it for
Open WebUI — so `docker compose up -d` here is for manual/debug work only, and
the container will not come back by itself after a reboot. See
`../litellm/README.md`.

## Layout

```text
/opt/stacks/comfyui/
├── compose.yml          # or compose.yaml
├── Dockerfile
├── .env
├── README.md
├── models/               # checkpoints, VAEs, LoRAs, ControlNets, embeddings
├── custom_nodes/         # ComfyUI extensions
├── output/               # generated images
├── input/                # img2img / ControlNet reference images
└── user/                 # workflows, settings, logs
```

Inside the container, ComfyUI lives at `/opt/ComfyUI`. The persistent host folders are mounted into its corresponding subdirectories.

## First-time setup

1. Create the persistent folders:

   ```bash
   sudo mkdir -p /opt/stacks/comfyui/{models,custom_nodes,output,input,user}
   ```

2. Confirm the host GPU device-group IDs:

   ```bash
   stat -c '%g' /dev/dri/card0
   stat -c '%g' /dev/dri/renderD128
   ```

   Put the results in `/opt/stacks/comfyui/.env`:

   ```env
   COMFYUI_PORT=8188
   LAN_IP=192.0.2.10
   VIDEO_GID=44
   RENDER_GID=992
   ```

   Replace `192.0.2.10` with the server's LAN IPv4 address. `44` and `992`
   are common group IDs, but the host values are authoritative.

3. Build and start the service:

   ```bash
   cd /opt/stacks/comfyui
   docker compose up -d --build
   docker compose logs -f comfyui
   ```

   The initial image build downloads a large ROCm/PyTorch base image and installs ComfyUI dependencies. Later starts normally need only `docker compose up -d`.

4. Verify PyTorch can use the GPU **before** loading a checkpoint:

   ```bash
   docker compose exec comfyui python3 - <<'PY'
   import torch
   print("Torch:", torch.__version__)
   print("HIP:", torch.version.hip)
   print("GPU available:", torch.cuda.is_available())
   print("GPU:", torch.cuda.get_device_name(0))
   PY
   ```

   It must print `GPU available: True` and identify the RX 9060 XT. If it does not, troubleshoot GPU passthrough/host ROCm before debugging ComfyUI or a model.

5. Open ComfyUI at [http://<your-LAN-IP>:8188](http://<your-LAN-IP>:8188).

## Models

The active generation/editing model is **Qwen-Image-2.1-Turbo**, described below.
Single-file SDXL checkpoints instead go in:

```text
/opt/stacks/comfyui/models/checkpoints/
```

An optional, previously tested SDXL starter is **Juggernaut XL v9**:

```bash
hf download RunDiffusion/Juggernaut-XL-v9 \
  Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors \
  --local-dir /opt/stacks/comfyui/models/checkpoints
```

Refresh the ComfyUI browser tab after adding a model. A container restart is not normally required.

### Qwen-Image-2.1-Turbo (active, int8)

7B MMDiT with native 2K output, RGBA/alpha, generation and editing in one
checkpoint, and up to 10 reference images. ComfyUI gained native support on
2026-09-20, so the image is pinned to that master commit via `COMFYUI_REF` in
`compose.yml`. The 16 GB card cannot hold the quantized UNet (~6.9 GB) and the
8B text encoder (~9.0 GB) resident at once, so ComfyUI runs with
`--disable-smart-memory` (set in `compose.yml`); without it the VAE decode OOMs
at the end of a run.

Download the active Turbo set:

```bash
hf download Comfy-Org/Qwen-Image-2.1 \
  diffusion_models/qwen_image_2.1_turbo_int8_convrot.safetensors \
  text_encoders/qwen3vl_8b_int8_convrot.safetensors \
  vae/qwen_image_2.1_vae_bf16.safetensors \
  --local-dir /opt/stacks/comfyui/models
```

That writes to `models/diffusion_models/`, `models/text_encoders/`, and
`models/vae/`. The int8 `_convrot` weights load natively on ROCm/gfx1200.

Keep the int8 UNet on this 31 GiB RAM / 16 GB VRAM host. Earlier bf16 base-UNet
tests (~14.2 GB plus the ~9 GB text encoder) hit host-memory pressure and died
mid-load with pinned memory enabled. The current configuration disables pinned
memory, but a bf16 upgrade has not been validated and is not the supported
default. Host RAM matters as well as VRAM. `compose.yml` deliberately does not set
`PYTORCH_CUDA_ALLOC_CONF=expandable_segments`: it is a no-op on ROCm (the
runtime logs `expandable_segments not supported on this platform`).

Use `../webui/workflows/qwen_image_2_1_turbo_{t2i,edit}_api.json` for the
active workflows. They use **Euler, CFG 1, and the official eight-step sigma
schedule**, not a generic `simple` schedule:
`1, 0.978453, 0.95418, 0.926626, 0.89508, 0.845148, 0.704534, 0.414568, 0`.
`ManualSigmas` fixes the step count; the MCP's compatibility `steps` argument
does not change it. Negative conditioning is unused at CFG 1.

Generation defaults to **2048×2048**; editing preserves the first input's
aspect ratio at roughly a **1024² pixel budget**, the intentional speed/memory
trade-off. Up to ten references are supported, with input alpha restored before
encoding. The proxy removes unused reference loaders and their alpha joins.
All four retained base/Turbo workflows passed live tests; the final MCP smoke
test confirmed default 2K generation and a 1024 edit with real PNG transparency.
This is a verified quality/stability balance for this host, not proof of an
absolute performance optimum. For RGBA output, wrap the prompt as
`This is an RGBA format image with transparency. <subject>. The image has an alpha channel and a transparent background.`
and save as PNG to keep the alpha channel. Inspect outputs: alpha support does
not guarantee perfect edges or eliminate model-generated background artifacts.

The base-model rollback pair `qwen_image_2_1_{t2i,edit}_api.json` retains
`euler` + `simple`, CFG 1, and 40 steps. It additionally requires
`diffusion_models/qwen_image_2.1_int8_convrot.safetensors` from the same
Comfy-Org repository. Do not apply the base model's 40-step advice to Turbo.

Peak VRAM is ~10 GB (the phases run sequentially: int8 text encoder ~9.9 GB,
then UNet ~8.1 GB, then VAE decode), leaving headroom on the 16 GB card.

The int8 text encoder is the encoder the official template ships with. It is
heavier and slower than `w4a8`; historically measured here with the Prompt Enhancer in front
(same prompt, byte-identical rewritten prompt, 20 steps at 1024×1024) int8 took
**168.5 s vs 123.3 s** for `w4a8` (~43 s more), and the two images were close
(PSNR 22.6 dB) with int8 looking slightly more realistic. int8 is kept as the
default for that reason; swap `text_encoders/qwen3vl_8b_w4a8.safetensors` into
each workflow's generation `CLIPLoader` to trade fidelity back for speed.

#### Direct prompts (no Prompt Enhancer)

The MCP now runs only the plain Turbo generation/edit workflows, with the
base-model pair retained for rollback. Prompt Enhancer workflows and the
`enhance` argument were removed: live tests showed the rewriter replacing
explicit transparency instructions with white/checkerboard backgrounds. This
also avoids its additional model load and autoregressive generation cost.

Previously downloaded PE checkpoints and custom-node files remain on disk but
are not used by these workflows. They are not required for a fresh deployment.
The existing conservative memory flags remain unchanged for stability on this
31 GB RAM / 16 GB VRAM machine.

#### Speed flags (historical backend comparison)

`compose.yml` passes `--use-ck-attention --enable-triton-backend`. Warm
base-model timings on the RX 9060 XT at 1024×1024; these are not current
Turbo end-to-end timings. The 8-step column is directly measured and the
25-step column is scaled from it unless noted:

| Config | 8 steps | ~25 steps |
|---|---|---|
| default attention | 14.6 s | ~40 s |
| `--use-pytorch-cross-attention` | 14.3 s | ~40 s |
| `--use-quad-cross-attention` | 17.2 s | ~48 s |
| `--use-split-cross-attention` | 19.7 s | ~55 s |
| `--use-ck-attention` | 12.6 s | ~35 s |
| `--use-ck-attention --enable-triton-backend` | 12.2 s | **28.5 s** (measured) |

`--fast` (comfy compiler) was measured to give no gain and adds a slow first
run, so it is not used. `--disable-smart-memory` is retained (see above); cold
starts are slower while models load from disk. The
attention timings above use the `w4a8` text encoder; with the int8 encoder add
roughly 14 s at 25 steps.

`--disable-pinned-memory --cache-none` remain conservative host-memory/cache
settings. They avoid page-locked host allocations and retained node outputs;
they were not retuned after PE removal. Current observed Turbo timings are in
`../comfyui-mcp/README.md` (about 60 s for warm 2K generation and 90 s for a
single-reference 1024 edit, varying with the prompt and model reloads).

Upstream references:
- [Comfy-Org model files and Turbo guidance](https://huggingface.co/Comfy-Org/Qwen-Image-2.1)
- [Official Turbo pipeline and sigma schedule](https://huggingface.co/Qwen/Qwen-Image-2.1-Turbo/blob/main/model_index.json)

Other model locations:

```text
models/vae/           # separate VAEs
models/loras/         # LoRAs
models/controlnet/    # ControlNet models
models/embeddings/    # textual inversions / embeddings
```

## Optional SDXL workflow (not the active default)

A previously tested alternative is standard SDXL text-to-image:

```text
Load Checkpoint
  ├─ MODEL ───────────────────────────────────────> KSampler
  ├─ CLIP ─> CLIP Text Encode (positive) ─────────> KSampler
  ├─ CLIP ─> CLIP Text Encode (negative) ─────────> KSampler
  └─ VAE ─────────────────────────────────────────> VAE Decode

Empty Latent Image ───────────────────────────────> KSampler
KSampler ─────────────────────────────────────────> VAE Decode ─> Save Image
```

A good baseline for Juggernaut XL v9:

| Setting | Starting value |
|---|---|
| Resolution | `1024×1024`, `832×1216`, or `1216×832` |
| Sampler | `dpmpp_2m` |
| Scheduler | `karras` |
| Steps | `30–40` |
| CFG | `5` (reasonable range: `3–7`) |
| Denoise | `1.0` for text-to-image |
| Batch size | `1` |

Keep the seed fixed while comparing sampler, CFG, or step-count changes. Change it to randomize only after selecting settings you like.

## Open WebUI integration

In Open WebUI, go to **Admin Panel → Settings → Images**:

- Engine: **ComfyUI**
- Base URL: `http://<your-LAN-IP>:8189/` (lifecycle proxy, not backend `8188`)
- API key: leave blank for local-network access

Use the committed Turbo API templates and node maps in `../webui/README.md`
for generation and editing. For a custom workflow, export it from ComfyUI:

1. Enable **Dev Mode** in Settings.
2. Load or build the desired workflow.
3. Use **Save (API Format)**, not the normal UI-format save.
4. Upload the resulting `workflow_api.json` as the ComfyUI workflow/model in Open WebUI.
5. Map Open WebUI's fields to the appropriate nodes; use the documented Turbo
   maps rather than mapping steps to its fixed `ManualSigmas` schedule.

Test from a new Open WebUI chat with a simple request such as: `a red apple on a wooden table, studio lighting`.

## Updating

To update ComfyUI and rebuild the stack:

```bash
cd /opt/stacks/comfyui
docker compose build --pull --no-cache
docker compose up -d
docker compose logs -f comfyui
```

Before updating a working image, record the currently working image ID and back up `compose.yml`, `Dockerfile`, `.env`, and important workflow JSON files. Add custom nodes one at a time and run a basic generation after each addition.

## Sharing the GPU

The RX 9060 XT has 16 GB VRAM. Avoid concurrent local llama.cpp and ComfyUI
workloads. The lifecycle proxy stops `llama-server`, `llama-companion`, and
`llama-bolt` before waking ComfyUI; LiteLLM stops ComfyUI before a local LLM
cold start. Use proxy port `8189` to preserve this coordination. Direct UI
starts on `8188` bypass it; stop local GPU tenants yourself for manual work.
Ollama currently serves cloud models only and does not hold the GPU. See
`../litellm/README.md` for lifecycle details.

## Troubleshooting

### GPU unavailable (`GPU available: False` or `No HIP GPUs are available`)

1. On the host, confirm device nodes exist:

   ```bash
   ls -l /dev/kfd /dev/dri
   ```

2. Confirm `VIDEO_GID` and `RENDER_GID` in `.env` match the host values.
3. Confirm the Compose file passes `/dev/kfd` and `/dev/dri` to the container.
4. Check the host AMDGPU/ROCm installation before changing ComfyUI settings.

### `HIP error: invalid device function` or a sampler crash

Do not change `HSA_OVERRIDE_GFX_VERSION` to an arbitrary value. Rebuild from the pinned Dockerfile and verify the PyTorch GPU test above. If the basic PyTorch test succeeds but ComfyUI crashes, capture logs from startup through the failure:

```bash
docker compose logs --tail=300 comfyui
```

Test a stock workflow with a standard checkpoint before adding custom nodes or advanced workflows.

### Out of memory

- Use batch size `1`.
- Reduce image size before enabling low-VRAM mode.
- Stop local llama.cpp or other GPU consumers; cloud-only Ollama is not a GPU tenant.
- `--disable-pinned-memory` is already enabled. Consider `--lowvram` only if
  normal operation genuinely runs out of VRAM; it trades speed for lower memory use.

### Workflow does not run from Open WebUI

- Confirm the workflow was exported in **API format**.
- Confirm Open WebUI can reach `http://<your-LAN-IP>:8189/` from its own container/network namespace.
- Confirm the prompt/node maps match `../webui/README.md`; Qwen uses
  `TextEncodeQwenImage21`, not SDXL's `CLIP Text Encode` nodes.

### Black or blank output

Verify that the checkpoint's VAE output is connected to `VAE Decode`. Only add a separate VAE if the checkpoint/workflow requires one; Juggernaut XL v9 supplies a VAE through its checkpoint loader.
