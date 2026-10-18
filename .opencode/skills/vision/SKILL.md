---
name: Vision
description: Inspect or compare image files when the current model cannot see images (for example a read returned "this model does not support image input", or art/screenshot review needs actual eyes). Dispatches the read-only vision subagent (Mistral Large 4 via the Mistral API) to describe the images and answer specific visual questions.
---

Use when a task needs an actual look at an image but the current model has
no vision — typical cases: art/sprite review gates, screenshot
verification, comparing asset versions, judging pose, readability, or
artifacts.

## Procedure

1. **Measure first, yourself.** Vision is not needed for numbers: use
   shell/uv Python for dimensions, alpha histograms, bounding boxes,
   palette membership, per-color pixel counts, and frame-to-frame diffs.
   Record those numbers — they are your evidence.
2. **Dispatch the `vision` subagent** for everything genuinely visual:
   composition, pose, identity, silhouette, artifacts, comparisons.
   Give it a self-contained prompt containing:
   - every image path to read (if an image lives outside the project
     workspace, copy it into the project first so the read is not
     blocked by external-directory approval);
   - context: what the asset is and which specs matter (palette hexes,
     identity markers, target pixel size);
   - precise, answerable questions ("Is the torso a strict 90° side
     profile?", "Do these four poses read as a run cycle?");
   - a request for a structured report and an explicit verdict.
3. **Combine both sources** in your review: numeric measurements from
   yourself, visual findings from the vision agent. Label which is which.
4. **The user stays the final approver** for art direction. The vision
   agent's verdict is evidence for review gates, not a replacement for
   the user's eyes.

## Notes

- `vision` is read-only (everything denied except read/glob/grep) and
  runs on `mistral/mistral-large-4#none` (no-thinking variant — quicker
  and cheaper per dispatch; trial, revert to `#high` if art-review
  quality drops). Per-token cost on the Mistral API. Keep one focused
  dispatch per review round; include several
  images in a single dispatch when comparing versions.
- Never let the vision agent be the only evidence for palette or alpha
  claims — measure those numerically.
