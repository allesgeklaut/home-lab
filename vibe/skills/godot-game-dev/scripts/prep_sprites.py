#!/usr/bin/env python3
"""Sprite prep pipeline for agent-generated game art (godot-game-dev skill).

Turns raw Qwen-Image output (1024², RGBA via the comfyui MCP) into game-ready
pixel-art assets:

  1. verify the alpha channel is real (not a baked opaque background)
  2. crop to the subject bounding box (sub-threshold alpha noise removed)
  3. downscale to the target size (area/BOX filter — keeps pixel clusters clean)
  4. quantise to a locked project palette (nearest colour, alpha threshold)
  5. anchor into a fixed canvas (bottom/centre) so frames share a pivot

Run with deps provided by uv, never installed system-wide:

  uv run --with pillow --with numpy python prep_sprites.py check <in.png>
  uv run --with pillow --with numpy python prep_sprites.py prep <in> <out> \
      --scale 0.0325 --canvas 40x40 --anchor bottom
  uv run --with pillow --with numpy python prep_sprites.py tiles <in> <out> \
      --tile 16 --gain 2.8
  uv run --with pillow --with numpy python prep_sprites.py band <in> <out> \
      --top 200 --height 320 --gain 1.35

Palette: pass --palette "#RRGGBB,#RRGGBB,..." (prep/tiles/band). The default is
the Neon-Ronin Pixel V1 palette; lock one palette per project and reuse it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image

# Neon-Ronin Pixel V1 (project default; override with --palette).
DEFAULT_PALETTE = [
    (0x0B, 0x0E, 0x1A),  # base dark — near-black indigo
    (0x1C, 0x23, 0x33),  # base mid — deep blue-grey
    (0x23, 0x27, 0x33),  # leather / cloth — charcoal
    (0x2A, 0x2D, 0x34),  # hair dark
    (0x55, 0x5A, 0x66),  # hair highlight
    (0x8A, 0x8D, 0x96),  # hair sides — grey
    (0xF2, 0xC8, 0x9B),  # skin light
    (0xE0, 0xA0, 0x75),  # skin shadow
    (0xE8, 0xB8, 0x4B),  # gold
    (0xFF, 0x2E, 0x88),  # neon magenta
    (0x2E, 0xE6, 0xFF),  # neon cyan
    (0xFF, 0xB3, 0x47),  # amber sparks
    (0x7E, 0xC8, 0xFF),  # day sky blue
    (0xFF, 0xFF, 0xFF),  # glint white
    # Derived extensions (texture / background tones — keep palettes limited):
    (0x14, 0x19, 0x28),  # dark slate — texture midtone
    (0x2E, 0x36, 0x4A),  # light slate — building highlights
    (0x3C, 0x2A, 0x4A),  # muted purple — night haze
    (0x9C, 0x2B, 0x63),  # dim magenta — glow falloff
    (0x1E, 0x6E, 0x80),  # dim cyan — glow falloff
]

PALETTE = list(DEFAULT_PALETTE)  # active palette, set from --palette


def parse_palette(spec: str) -> list[tuple[int, int, int]]:
    colors: list[tuple[int, int, int]] = []
    for tok in spec.replace(" ", "").split(","):
        if not tok:
            continue
        tok = tok.lstrip("#")
        if len(tok) != 6:
            raise SystemExit(f"bad palette colour: {tok!r} (need #RRGGBB)")
        colors.append((int(tok[0:2], 16), int(tok[2:4], 16), int(tok[4:6], 16)))
    if not colors:
        raise SystemExit("empty palette")
    return colors


def load_rgba(path: Path) -> np.ndarray:
    im = Image.open(path).convert("RGBA")
    return np.array(im, dtype=np.uint8)  # writable copy


def alpha_report(arr: np.ndarray) -> str:
    a = arr[..., 3]
    total = a.size
    transparent = int((a < 16).sum())
    opaque = int((a > 240).sum())
    return (
        f"alpha: {transparent / total:.1%} transparent, {opaque / total:.1%} opaque "
        f"(min={int(a.min())}, max={int(a.max())})"
    )


def cmd_check(args: argparse.Namespace) -> int:
    arr = load_rgba(Path(args.image))
    print(alpha_report(arr))
    a = arr[..., 3]
    frac = (a < 16).mean()
    if frac < 0.02:
        print("FAIL: almost no transparency — background is baked in", file=sys.stderr)
        return 1
    print("OK: transparency present")
    return 0


def quantise(arr: np.ndarray, alpha_cut: int = 128) -> np.ndarray:
    """Snap RGB to the active palette; hard-edge the alpha for pixel art.

    int32 distances are mandatory: int16 squares overflow and silently match
    wrong palette entries (symptom: random neon colours on dark art).
    """
    rgb = arr[..., :3].astype(np.int32)
    alpha = arr[..., 3]
    pal = np.asarray(PALETTE, dtype=np.int32)
    d = ((rgb[:, :, None, :] - pal[None, None, :, :]) ** 2).sum(-1)
    idx = d.argmin(-1)
    out = np.zeros_like(arr)
    out[..., :3] = pal[idx]
    out[..., 3] = np.where(alpha >= alpha_cut, 255, 0).astype(np.uint8)
    return out


def crop_bbox(arr: np.ndarray, pad: int = 0, thresh: int = 16) -> np.ndarray:
    """Crop to visible pixels. Threshold first — alpha 1..15 noise from the
    generator inflates the bbox to (nearly) the full canvas otherwise."""
    alpha = arr[..., 3]
    ys, xs = np.where(alpha > thresh)
    if len(xs) == 0:
        raise SystemExit("image has no visible pixels")
    y0, y1 = max(0, ys.min() - pad), min(arr.shape[0], ys.max() + 1 + pad)
    x0, x1 = max(0, xs.min() - pad), min(arr.shape[1], xs.max() + 1 + pad)
    return arr[y0:y1, x0:x1]


def cmd_prep(args: argparse.Namespace) -> int:
    src = Path(args.image)
    dst = Path(args.out)
    arr = load_rgba(src)
    print(f"{src.name}: {arr.shape[1]}x{arr.shape[0]} — {alpha_report(arr)}")
    arr[..., 3] = np.where(arr[..., 3] < 16, 0, arr[..., 3])

    arr = crop_bbox(arr)
    h, w = arr.shape[:2]

    if args.scale:
        # fixed scale across frames keeps proportions consistent
        tw, th = max(1, round(w * args.scale)), max(1, round(h * args.scale))
    else:
        scale = args.height / h if args.height else None
        if args.width and (scale is None or w * scale > args.width):
            scale = args.width / w
        if scale is None:
            raise SystemExit("need --height, --width or --scale")
        th = args.height
        tw = max(1, round(w * (args.height / h)))
        if args.width and tw > args.width:
            tw = args.width
            th = max(1, round(h * (args.width / w)))

    im = Image.fromarray(arr)
    # two-step area downscale keeps clusters tidy at big reduction factors
    big = im.resize((tw * 2, th * 2), Image.BOX)
    small = big.resize((tw, th), Image.BOX)
    out = quantise(np.asarray(small, dtype=np.uint8))

    cw, ch = (int(v) for v in args.canvas.lower().split("x")) if args.canvas else (tw, th)
    canvas = np.zeros((ch, cw, 4), dtype=np.uint8)
    x_off = (cw - tw) // 2
    y_off = ch - th if args.anchor == "bottom" else (ch - th) // 2
    h_ = min(th, ch)
    w_ = min(tw, cw)
    y_off = max(0, y_off)
    x_off = max(0, x_off)
    canvas[y_off:y_off + h_, x_off:x_off + w_] = out[:h_, :w_]

    dst.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(canvas).save(dst)
    print(f"-> {dst}  ({cw}x{ch}, content {tw}x{th}, anchor={args.anchor})")
    return 0


def cmd_tiles(args: argparse.Namespace) -> int:
    """Build a 2-tile tileset from one generated texture.

    Output: 32x16 PNG — tile 0 = fill, tile 1 = surface (bright rim on top).
    Textures usually need --gain (2.5-3.0 for dark night textures) before
    quantise, or they flatten to the palette's darkest colour.
    """
    arr = load_rgba(Path(args.image))
    ts = args.tile
    if args.gain != 1.0:
        rgb = arr[..., :3].astype(np.float32) * args.gain
        arr = arr.copy()
        arr[..., :3] = np.clip(rgb, 0, 255).astype(np.uint8)
    cy, cx = arr.shape[0] // 2, arr.shape[1] // 2
    crop = arr[cy - ts // 2: cy + ts // 2, cx: cx + ts]
    if args.offset:
        ox, oy = (int(v) for v in args.offset.split(","))
        crop = arr[cy - ts // 2 + oy: cy + ts // 2 + oy, cx + ox: cx + ts + ox]
    fill = quantise(crop)

    surface = fill.copy()
    rim = np.array([(0x2E, 0xE6, 0xFF, 255), (0x1A, 0x8A, 0xA6, 255)], dtype=np.uint8)
    surface[0, :] = rim[0]
    surface[1, :] = rim[1]

    sheet = np.zeros((ts, ts * 2, 4), dtype=np.uint8)
    sheet[:, :ts] = fill
    sheet[:, ts:] = surface
    dst = Path(args.out)
    dst.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(sheet).save(dst)
    print(f"-> {dst} (2 tiles of {ts}x{ts}: fill | surface)")
    return 0


def cmd_band(args: argparse.Namespace) -> int:
    """Crop a horizontally-tileable band out of a big background panorama.

    Height must cover the on-screen range plus margins; position the band in
    the scene so its cropped top/bottom edges sit off-screen. A mild --gain
    (1.2-1.4) counteracts palette-quantise darkening.
    """
    arr = load_rgba(Path(args.image))
    if args.gain != 1.0:
        rgb = arr[..., :3].astype(np.float32) * args.gain
        arr = arr.copy()
        arr[..., :3] = np.clip(rgb, 0, 255).astype(np.uint8)
    h = arr.shape[0]
    band_h = args.height
    top = int(args.top) if args.top is not None else (h - band_h) // 2
    band = arr[max(0, top): max(0, top) + band_h, :]
    q = quantise(band, alpha_cut=1)  # opaque art: keep everything
    if args.width:
        q = q[:, : args.width]
    dst = Path(args.out)
    dst.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(q).save(dst)
    print(f"-> {dst} ({q.shape[1]}x{q.shape[0]})")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="alpha sanity check")
    c.add_argument("image")
    c.set_defaults(fn=cmd_check)

    s = sub.add_parser("prep", help="crop/scale/quantise/anchor a sprite")
    s.add_argument("image")
    s.add_argument("out")
    s.add_argument("--height", type=int, default=0, help="target content height in px")
    s.add_argument("--width", type=int, default=0, help="max content width in px")
    s.add_argument("--scale", type=float, default=0.0, help="fixed scale factor (overrides height/width)")
    s.add_argument("--canvas", default=None, help="fixed canvas WxH, e.g. 40x40")
    s.add_argument("--anchor", choices=["bottom", "center"], default="bottom")
    s.add_argument("--palette", default="", help='comma-separated #RRGGBB list')
    s.set_defaults(fn=cmd_prep)

    t = sub.add_parser("tiles", help="texture -> 2-tile tileset")
    t.add_argument("image")
    t.add_argument("out")
    t.add_argument("--tile", type=int, default=16)
    t.add_argument("--offset", default=None, help="crop offset x,y from centre")
    t.add_argument("--gain", type=float, default=1.0, help="brightness multiplier before quantise")
    t.add_argument("--palette", default="")
    t.set_defaults(fn=cmd_tiles)

    b = sub.add_parser("band", help="crop a band out of a panorama")
    b.add_argument("image")
    b.add_argument("out")
    b.add_argument("--top", type=int, default=None)
    b.add_argument("--height", type=int, required=True)
    b.add_argument("--width", type=int, default=0)
    b.add_argument("--gain", type=float, default=1.0, help="brightness multiplier before quantise")
    b.add_argument("--palette", default="")
    b.set_defaults(fn=cmd_band)

    args = p.parse_args()
    if getattr(args, "palette", ""):
        global PALETTE
        PALETTE = parse_palette(args.palette)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
