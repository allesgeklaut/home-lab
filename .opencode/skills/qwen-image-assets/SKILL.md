---
name: qwen-image-assets
description: Generate and edit images with the local Qwen-Image-2.1 (ComfyUI) pipeline — native transparent RGBA assets, reference-image chaining, and instruction-based edits. Use when creating or editing any image asset (game sprites, transparent cut-outs, character turnarounds/variants, textures, UI art, concept art) or when asked to generate or edit a picture.
---

# Qwen-Image-2.1 image assets

Create and edit images through the local **`comfyui` MCP** server, which runs
Qwen-Image-2.1 on the on-demand ComfyUI container. The model does text-to-image
and instruction editing in one 7B unified model, and it outputs **native RGBA** —
transparent backgrounds come out of the diffusion process, not a post-hoc
background-removal pass.

**Use this skill whenever an image is being produced or altered.** Do not
hand-write SVG/canvas stand-ins when a real raster asset is wanted.

## Tools

| opencode tool | Purpose |
| --- | --- |
| `comfyui_image_status` | Is ComfyUI up? which models are loaded? Cheap — never starts the GPU. |
| `comfyui_generate_image` | Text → image. Args: `prompt`, `negative_prompt`, `width`, `height`, `steps`, `seed`. |
| `comfyui_edit_image` | Instruction edit of an existing local file. Args: `image_path`, `prompt`, `steps`, `seed`. |

(In bare MCP clients the names are `image_status` / `generate_image` /
`edit_image`.)

Both image tools return the saved path(s) under `/opt/stacks/comfyui/output`
**and** the image itself, so you can look at the result before accepting it.

**Timing**: ComfyUI cold-starts on demand — first call ~1 min, warm calls
~40–50 s, edits ~150 s (the reference image goes through the text encoder too).
Give the MCP timeout room; a cold call is normal, not a hang.

## Capability notes (Qwen-Image-2.1)

- **Native RGBA** generation, at 2K native — but the local workflow is fixed at
  1024×1024 by default. `comfyui_generate_image` can change `width`/`height`.
- **Up to 10 reference images** per request, in prompt order ("the first image",
  "the second image"). Fidelity drops as references are added — stay at or below
  ~4 when detail matters. Landscape ratios (3:2, 16:9) work better when
  combining several subjects.
- **Instruction editing** plus local edits guided by circles / painted marks /
  masks. Can also extract a subject from a photo into an RGBA layer.
- **`steps`** 40 is the model's official pipeline; 25 is the floor. Raise it only
  if an asset is falling apart.
- `cfg` is **fixed at 1** in the local workflow, which is the model's official
  path — and at `cfg=1` the **negative prompt is ignored**. See "Local
  limitations" below.
- Open-weights model; check its current license before commercial use.

## The one rule that matters: transparency is prompt-driven

There is **no `background_type` switch** in the local workflow. A transparent
result comes from the prompt. Two behaviours to internalise:

1. **Describe only the subject.** Any mention of a scene, surface, backdrop,
   floor, paper, or gradient can make the model render it — and the output comes
   back opaque.
2. **Always append the background clause verbatim**, at the end of the prompt:

> TRUE ALPHA transparent background — alpha channel only. No background layer, no
> colour fill, no paper texture, no parchment, no backdrop, no gradient, no
> vignette, no scenery, no frame, no border, no checkerboard pattern, no cast
> shadow, no ground plane, no text, no watermark. Subject only, isolated on
> genuine transparency.

The model's own recommended phrasing is also worth including up front:
*"This is an RGBA image with transparency. … The image has alpha channel and the
background is transparent."*

A minimal transparent-asset prompt:

```
This is an RGBA image with transparency.
<subject: one or two sentences, silhouette-first, concrete style/colour words>.
<subject again, short>.
TRUE ALPHA transparent background — alpha channel only. No background layer, no
colour fill, no paper texture, no parchment, no backdrop, no gradient, no
vignette, no scenery, no frame, no border, no checkerboard pattern, no cast
shadow, no ground plane, no text, no watermark. Subject only, isolated on
genuine transparency.
```

## Reference chaining — the biggest lever against drift

Consistency across generated assets comes from **feeding the canonical art back
in**, not from repeating adjectives. Chain references through a sequence of
single-reference edits:

1. Generate or pick the **canonical** asset (front view / turnaround).
2. For each new frame, call `comfyui_edit_image` with the canonical image as
   `image_path` and an instruction like *"Same character, same style, same
   palette. Change to: <new pose/direction/state>. Keep the identity marks
   (<per-asset identity markers>) exactly. TRUE ALPHA transparent background …"*.
3. Feed the **accepted** result forward as the reference for the next frame, so
   drift cannot accumulate unchecked.

Re-state the small set of **identity markers** in every prompt. They are what
survive when everything is reduced to flat colours.

## Local limitations (and how to work around them)

- **`negative_prompt` is ignored at `cfg=1`.** Do not rely on it. Express
  exclusions **positively** in the main prompt ("plain", "single subject",
  "isolated") and, above all, by **reference chaining**. If you truly need
  negatives, the workflow's `cfg` would have to be raised — but 1 is the
  model's official setting, so prefer prompt + references.
- **`comfyui_edit_image` accepts one reference image** and always returns
  **1024×1024**, regardless of input size. Multi-reference editing (up to 10)
  needs the workflow extended: `TextEncodeQwenImage21` already exposes
  `images.image_1`; add `LoadImage` nodes wired as `images.image_2` … and the
  server would need to pass them. Until then, chain single-reference edits.
- ComfyUI is cold-started by the lifecycle proxy; there is no need to manage it.

## Verify before accepting — always

Generated files are **not** game-ready, and an RGBA file can still be fully
opaque. Always:

1. **Check the alpha channel is real** (not a dirty/opaque alpha):

   ```bash
   python3 /opt/stacks/.opencode/skills/qwen-image-assets/scripts/check_alpha.py /opt/stacks/comfyui/output/<file>.png
   ```

   It prints transparency stats and exits non-zero for an opaque or empty image.
2. **Look at the asset** — the tool returns the image inline; inspect it for
   artefacts, extra limbs, text, or a baked-in background.
3. **Pass the silhouette test** (project-specific, if the project has one):
   the asset must still read as a clean shape in flat black.
4. **Downscale + quantise to the target palette** before use. Generated art is
   1024²; a game running at 640×360 needs a project-provided slicer. In
   `game-1` that is `tools/slice_knight.py` (flood-fill background removal → 2×
   downscale → palette quantise → anchored canvas) and `tools/gen_knight_frames.py`.
5. Wire the result into the project's `.tscn`/manifest only after 1–4 pass.

## Workflow checklist

- [ ] Decide: text-to-image, or edit-an-existing-image (reference chain)?
- [ ] Prompt names only the subject, with concrete style/silhouette words.
- [ ] Background clause appended **verbatim**.
- [ ] Identity markers restated (if a character/series).
- [ ] Reference image passed for anything that must match prior art.
- [ ] `steps` 40, seed left random unless reproducibility is needed.
- [ ] `check_alpha.py` passes; the returned image visually inspected.
- [ ] Palette-quantised and downscaled by the project's pipeline before wiring in.

## Project hook: game-1

This repo is a top-down pixel-art ARPG. Its locked art direction (Ink/Woodcut
palette, the Knight's identity markers, world-fixed top-down lighting) lives in
the project's `AGENTS.md` and `docs/art-style-bible-ink-woodcut.md`. **Read them
before generating any character asset** and reuse the canonical references
(`images/knight.png`, `images/knight_top.png`, `images/knight_top_front.png`)
via reference chaining.
