---
name: godot-game-dev
description: Build, edit, and playtest Godot games with agents — drive the godot-ai MCP (run, see, control, verify), simulate input, persist screenshots, wire ComfyUI-generated pixel art into a project, and bootstrap new game projects. Use for any Godot game task (2D or 3D) — new projects, scenes, gameplay scripts, AI-generated sprites and art, platformers, or when a game must be run, tested, or verified on screen.
---

# Godot agent game-dev (godot-ai + comfyui)

Proven end-to-end on this machine: agents can create a Godot project, generate
its art locally, implement gameplay, run the game, see it, drive it with
simulated input, and verify state — all through MCP tools. This skill distills
the playbook. The reference implementation is
`/home/johannes/projects/neon-ronin` (read its `AGENTS.md` and scripts for a
complete working example).

## The core loop

1. **Edit** — scenes (`.tscn`) and scripts (`.gd`) are text. Prefer
   **code-authored** content: build levels/tilesets/entities in `_ready()`
   from data structures (e.g. an ASCII level map). It is diffable, reviewable,
   and trivially agent-editable; hand-authored `.tscn` node trees are fragile.
2. **Validate** — always before running:
   `godot --headless --import` (from the project dir). Fix every script error.
   For quick logic checks: `timeout 30 godot --headless --quit-after 180`.
3. **Run** — `project_run` waits for game liveness (`game_status.status` =
   "live", `helper_live` = true).
4. **See** — `editor_screenshot` with `source: "game"` (returns the frame
   inline; never persists — see "Screenshots" below).
5. **Control** — `game_manage` with `input_sequence` (frame-timed, one call,
   focus-independent). Use separate `input_key`/`input_action` calls only for
   non-timing-sensitive steps.
6. **Verify** — `game_eval` (explicit `return`; `await` works inside) or
   `get_ui_elements`. Assert exact numbers, not "looks right".
7. **Logs** — `logs_read` `source: "editor" | "game" | "all"`, with
   `include_details: true` when debugging. Boot-time parse errors appear in
   the **editor** log, never the game log. Watch `new_errors_since_last_call`
   in tool responses — it surfaces silent failures (even for evals).

## Runtime contracts (all hard-won — do not relearn these)

- **Simulated input sets action state, not events.** `input_sequence` /
  `input_action` never emit `InputEvent`s: `_unhandled_input` sees nothing,
  and `Input.is_action_just_pressed` is unreliable for simulated presses.
  Gameplay code must **poll** `Input.is_action_pressed` and do **manual edge
  detection** (track the previous held state). Real keyboards still work
  through the same polling.
- **`input_sequence`** takes
  `steps: [{at_frame, action, pressed, strength?}]` + `settle_frames`; steps
  must be non-decreasing in `at_frame`; it cannot run inside `batch_execute`.
- **`input_mouse`**: press **and** release pairs (one event per call); the
  `position` must be a nested `{"x","y"}` dict — flat args are ignored.
- **`game_eval`**: the code is wrapped in a function — use an explicit
  `return`. `await` works inside (`await get_tree().create_timer(0.5).timeout`,
  `await get_tree().process_frame`), which is perfect for staging and waiting
  in one call. Return small values only.
- **Staging interactions is legitimate**: e.g. set
  `player.global_position` above a drone, then poll frames until contact — far
  more robust than timing input by hand. Combine staged physics with real
  `input_sequence` tests.
- **Area2D contact events resolve up to a physics step late.** Never predicate
  game logic on sub-frame position margins (e.g. "player feet 2px above drone
  top"): the event can fire several pixels into the overlap. Compare body
  **centers** instead (a player with origin at the feet: center = `y - half_height`).
- **Editor must not be playing for writes.** Scene/resource/file edits fail
  with `EDITOR_NOT_READY / sub_code=EDITOR_PLAYING` — call
  `project_manage(op="stop")`, then retry.
- **One editor session per port** (8000 HTTP / 9500 WS default). To switch
  projects, close the old editor; if `session_manage(op="list")` shows a stale
  session and the port is held by an orphan `godot_ai` python server, stop
  that process too. The plugin self-spawns the server on editor launch, and
  `attach` (the MCP entry) targets the port.
- **A real display is required for real screenshots.** Headless/dummy display
  renders nothing. Launch the editor on the user's display for playtests
  (see Environment).
- **`editor_screenshot` is inline-only.** It returns image content to the
  client and writes no file.

### Screenshots that persist

Capture from *inside* the game (full framebuffer resolution):

```gdscript
# game_eval code — save a shot to disk
var img := get_viewport().get_texture().get_image()
var err := img.save_png("/abs/path/shots/01_start.png")
return {"err": err}
```

- **Create the directory first** — `save_png` does NOT create directories and
  fails with a non-zero `err` (the eval itself still succeeds — always check
  the returned `err`).
- Stage the moment before saving (set position/velocity, wait frames until the
  event, then capture) — e.g. mid-jump, stomp burst, win overlay.
- Reference shots belong in the project (`docs/screenshots/`), committed.

## Project conventions that make agent dev smooth

- **Renderer `gl_compatibility`** on this machine (GPU has no Vulkan). Set in
  `project.godot` for both desktop and mobile.
- **Pixel art**: small viewport with integer scaling
  (`window/size/viewport_width/height`, `window_width_override`,
  `window/stretch/mode="canvas_items"`, `scale_mode="integer"`), nearest
  texture filtering (`textures/canvas_textures/default_texture_filter=0`).
  384x216 viewport at 3x (1152x648 window) worked well.
- **Player origin at the feet**; sprite canvases bottom-anchored
  (`Sprite2D.offset.y = -canvas_h/2` for a centered sprite). All animation
  frames of one character share **one canvas size and one scale factor** so
  proportions/pivots match.
- **TileSet built in code**: add the `TileSetAtlasSource` to the `TileSet`
  **before** `source.create_tile()` — otherwise `get_tile_data()` has no
  physics layers and collision calls error. Then
  `td.add_collision_polygon(0)` + `set_collision_polygon_points(...)` per tile.
- **Auto-tiled rims**: pick the "surface" tile variant when the cell below is
  empty, "fill" otherwise (bake a bright edge into the surface tile — cheap,
  effective readability for platforms).
- **CPUParticles2D** needs a 1x1 white texture:
  `Image.create_empty(1,1,false,Image.FORMAT_RGBA8)` + `img.fill(Color.WHITE)`
  (a transparent default texture makes particles invisible); free on
  `finished.connect(p.queue_free)` for one-shots.
- **Parallax2D** (`scroll_scale` e.g. `(0.35, 0.0)`) for backgrounds; position
  bands so their cropped edges sit off-screen.
- **Camera**: `Camera2D` child of the player + `limit_*` set from level size;
  the level's vertical range usually fixes the view.
- Keep one clean git commit per verified milestone; never commit `.godot/`.

## Art into the game (comfyui → sprites)

Generation itself: follow the **`qwen-image-assets` skill** (always append the
verbatim TRUE ALPHA clause for sprites; reference-chaining preserves identity;
concurrent generate/edit calls are fine).

Post-process with this skill's `scripts/prep_sprites.py` (run via
`uv run --with pillow --with numpy` — never install system-wide):

```sh
P="uv run --with pillow --with numpy python <skill>/scripts/prep_sprites.py"

$P check  assets/raw/hero_idle.png                     # alpha sanity (real transparency?)
$P prep   assets/raw/hero_idle.png assets/art/sprites/hero_idle.png \
          --scale 0.0325 --canvas 40x40 --anchor bottom
$P tiles  assets/raw/asphalt.png assets/art/tiles/rooftop_tiles.png --tile 16 --gain 2.8
$P band   assets/raw/city.png assets/art/bg/city_band.png --top 200 --height 320 --gain 1.35
```

- Palette comes from `--palette "#0B0E1A,#1C2333,..."` (default: the
  Neon-Ronin V1 palette — lock a project palette and reuse it everywhere).
- **Use one `--scale` for all frames of a character** (compute it from the
  canonical frame: `target_height / bbox_height`).
- Textures/backgrounds want full coverage + a `--gain` lift before quantise;
  sprites want true alpha.
- New-file anchors of the canvas: `bottom` for ground characters/props,
  `center` for pickups/flyers.
- **Always preview before wiring in**: 8x nearest-neighbour upscale on a
  checkerboard (transparency + cluster quality). Verify alpha with `check`.
- Pipeline pitfalls: sub-threshold alpha noise (alpha < ~16) inflates
  bounding boxes — threshold before measuring; and compute palette distances
  in int32 (int16 overflows and silently matches wrong colors).

## Verification discipline (what "tested" means)

- Assert with **numbers**: positions, velocities, counts — "blocked at exactly
  x=139", "landed on platform top y=160.0", "bounce velocity -210", "drones
  3 → 2". Screenshot + eval together are solid evidence; either alone is not.
- Test the full input path the user uses: run → collect → jump → interact →
  win/fail → restart. Include the death/respawn path.
- After the E2E: leave the game running if the user wants to play, or do a
  graceful close (`project_manage(op="stop")`, `editor_manage(op="quit")`).

## Bootstrapping a new Godot project (checklist)

1. `project.godot`: name, main scene, features `4.7`+`GL Compatibility`,
   autoload `_mcp_game_helper="*res://addons/godot_ai/runtime/game_helper.gd"`,
   editor plugin `res://addons/godot_ai/plugin.cfg` enabled, display/rendering
   settings from "Project conventions", input actions.
2. Copy `addons/godot_ai` from an existing project (keeps MCP plugin versions
   consistent) — or install godot-ai per its docs.
3. `godot --headless --import` must be clean.
4. Launch the editor **on the user's display**, wait for the session
   (`session_manage(op="list")`), then work the core loop.
5. Write a project `AGENTS.md` (art direction, conventions, runtime
   contracts) — agents pick it up automatically.

## Local environment (this machine)

- Godot 4.7.x at `~/.local/bin/godot`; `uv` for Python tooling.
- Editor launch (game window + screenshots need this display):
  `DISPLAY=:1 XAUTHORITY=/run/user/1000/gdm/Xauthority godot --path <project> --editor`
- godot-ai MCP entry pinned in `/opt/stacks/opencode.json`
  (`uvx … godot-ai attach --port 8000 --ws-port 9500`).
- ComfyUI MCP + outputs: `/opt/stacks/comfyui/output/` (see the
  `qwen-image-assets` skill).
- Game art-direction docs live in **Trilium**; project-specific rules in each
  project's `AGENTS.md`.

## Reference implementation

`/home/johannes/projects/neon-ronin` — Neon Ronin platformer prototype:
- `scripts/game.gd` — ASCII level map + code-built TileSet/entities/HUD/particles
- `scripts/{player,drone,shard}.gd` — character controller w/ coyote+buffer,
  stompable enemy, collectible
- `tools/prep_sprites.py` — the same pipeline as `scripts/prep_sprites.py` here
- `docs/screenshots/` — committed in-game captures
- `AGENTS.md` — art direction + runtime contracts
