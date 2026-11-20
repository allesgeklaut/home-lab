#!/usr/bin/env python3
"""Verify that an image really has a usable alpha channel.

Qwen-Image-2.1 generates native RGBA, but a prompt that mentions a
background/scene/surface can come back opaque with a "dirty" alpha channel
(all 255). This checks the actual pixel data instead of trusting the file
extension.

Usage:
    python3 check_alpha.py IMAGE [IMAGE ...]

Exit code 0 only when every image has an alpha channel and at least some
fully transparent pixels (i.e. a cut-out background).
"""

import sys
from PIL import Image


def report(path: str) -> bool:
    try:
        im = Image.open(path)
    except Exception as exc:  # noqa: BLE001 - surface any read failure
        print(f"ERROR {path}: {exc}")
        return False

    has_alpha = im.mode in ("RGBA", "LA") or "transparency" in im.info
    if not has_alpha:
        print(f"FAIL  {path}: mode={im.mode}, no alpha channel (opaque image)")
        return False

    a = im.convert("RGBA").getchannel("A")
    hist = a.histogram()
    total = im.width * im.height
    transparent = hist[0]
    opaque = hist[255]
    partial = total - transparent - opaque
    bbox = a.point(lambda p: 255 if p > 0 else 0).getbbox()

    print(
        f"      {path}: {im.width}x{im.height} mode={im.mode} "
        f"transparent={transparent} ({100 * transparent / total:.1f}%) "
        f"partial={partial} opaque={opaque} opaque_bbox={bbox}"
    )

    if transparent == 0:
        print(f"FAIL  {path}: alpha channel is fully opaque (no cut-out background)")
        return False
    if bbox is None:
        print(f"FAIL  {path}: image is fully transparent (empty)")
        return False

    print(f"OK    {path}: real transparency present")
    return True


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__.strip())
        return 2
    ok = True
    for path in sys.argv[1:]:
        ok = report(path) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
