"""Convert an MP4 to an animated GIF using PyAV + Pillow inside the ComfyUI container.

Run via:
  docker exec -i comfyui python3 - /path/in.mp4 /path/out.gif 640 < mp4_to_gif.py
"""

import sys

import av
from PIL import Image

src, dst = sys.argv[1], sys.argv[2]
max_width = int(sys.argv[3]) if len(sys.argv) > 3 else 640

container = av.open(src)
stream = container.streams.video[0]
fps = float(stream.average_rate) if stream.average_rate else 24.0
frames = []
for frame in container.decode(stream):
    img = frame.to_image().convert("RGB")
    if img.width > max_width:
        height = round(img.height * max_width / img.width)
        img = img.resize((max_width, height), Image.LANCZOS)
    frames.append(img)
container.close()

if not frames:
    raise SystemExit("no frames decoded")

# GIF stores centiseconds. Distribute rounding instead of truncating every
# 24-fps frame to 40 ms, which would speed the entire animation up by ~4%.
durations_ms = [
    max(10, (round((i + 1) * 100 / fps) - round(i * 100 / fps)) * 10)
    for i in range(len(frames))
]
frames[0].save(
    dst,
    save_all=True,
    append_images=frames[1:],
    duration=durations_ms,
    loop=0,
    optimize=True,
)
print(f"wrote {dst}: {len(frames)} frames, {sum(durations_ms)} ms total, {frames[0].size[0]}x{frames[0].size[1]}")
