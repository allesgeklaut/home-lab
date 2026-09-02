# ComfyUI (RDNA 4 / RX 9060 XT)

Local image generation on the pop os server (`192.168.0.46`), using the AMD RX 9060 XT 16 GB through ROCm.

This stack deliberately builds a local ComfyUI image from a **pinned AMD ROCm/PyTorch base**. It does not use the generic `rocm/comfyui` image and does not rely on a `latest` tag or changes made manually inside a running container.

## Stack

- GPU: AMD Radeon RX 9060 XT, 16 GB (RDNA 4 / gfx1200)
- Base image: `rocm/pytorch:rocm7.2.1_ubuntu24.04_py3.12_pytorch_release_2.9.1`
- Runtime: Docker Compose with `/dev/kfd` and `/dev/dri` passed through
- UI: ComfyUI on port `8188`
- Persistent data: host bind mounts under `/opt/stacks/comfyui/`

`HSA_OVERRIDE_GFX_VERSION` is intentionally **not** set. The RX 9060 XT is native gfx1200; use ROCm's normal hardware detection first rather than forcing a different architecture.

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
   VIDEO_GID=44
   RENDER_GID=992
   ```

   `44` and `992` are common values, but the host values are authoritative.

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

5. Open ComfyUI at [http://192.168.0.46:8188](http://192.168.0.46:8188).

## Models

Place checkpoint files in:

```text
/opt/stacks/comfyui/models/checkpoints/
```

The working starter model is **Juggernaut XL v9**:

```bash
hf download RunDiffusion/Juggernaut-XL-v9 \
  Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors \
  --local-dir /opt/stacks/comfyui/models/checkpoints
```

Refresh the ComfyUI browser tab after adding a model. A container restart is not normally required.

Other model locations:

```text
models/vae/           # separate VAEs
models/loras/         # LoRAs
models/controlnet/    # ControlNet models
models/embeddings/    # textual inversions / embeddings
```

## Basic SDXL workflow

The currently proven workflow is standard SDXL text-to-image:

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
- Base URL: `http://192.168.0.46:8188/`
- API key: leave blank for local-network access

Then in ComfyUI:

1. Enable **Dev Mode** in Settings.
2. Load or build the desired workflow.
3. Use **Save (API Format)**, not the normal UI-format save.
4. Upload the resulting `workflow_api.json` as the ComfyUI workflow/model in Open WebUI.
5. Map Open WebUI's prompt, checkpoint, steps, and size fields to the appropriate workflow nodes.

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

The RX 9060 XT has 16 GB VRAM. Avoid concurrent heavy Ollama and ComfyUI workloads: they compete for VRAM and can cause slowdowns or out-of-memory failures.

For long image-generation jobs, stop the LLM service first if necessary:

```bash
cd /opt/stacks/ollama && docker compose stop
cd /opt/stacks/comfyui && docker compose up -d
```

Start Ollama again when finished:

```bash
cd /opt/stacks/ollama && docker compose up -d
```

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
- Stop Ollama or other GPU consumers.
- Use `--lowvram --disable-pinned-memory` only if normal operation genuinely runs out of VRAM; it trades speed for lower memory use.

### Workflow does not run from Open WebUI

- Confirm the workflow was exported in **API format**.
- Confirm Open WebUI can reach `http://192.168.0.46:8188/` from its own container/network namespace.
- Confirm the prompt text node selected in Open WebUI is connected to the positive `CLIP Text Encode` node.

### Black or blank output

Verify that the checkpoint's VAE output is connected to `VAE Decode`. Only add a separate VAE if the checkpoint/workflow requires one; Juggernaut XL v9 supplies a VAE through its checkpoint loader.

