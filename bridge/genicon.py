"""Generate a 224x224 PNG icon for the Yandex skill (no external deps)."""

import struct
import zlib
from pathlib import Path

SIZE = 224
BG = (0x5B, 0x3F, 0xD6)      # deep violet
FG = (0xFF, 0xFF, 0xFF)      # white
ACCENT = (0x9E, 0x8C, 0xFF)  # light violet

rows = []
cx = cy = SIZE / 2

for y in range(SIZE):
    row = bytearray()
    for x in range(SIZE):
        dx, dy = x - cx, y - cy
        dist = (dx * dx + dy * dy) ** 0.5
        color = BG
        # robot body: rounded rectangle in the middle
        if 60 <= x <= 164 and 78 <= y <= 150:
            color = FG
        # lidar turret
        if dist <= 26:
            color = ACCENT
        if dist <= 14:
            color = FG
        # two "eyes" (sensors)
        if (78 <= x <= 92 or 132 <= x <= 146) and 104 <= y <= 118:
            color = BG
        row += bytes(color)
    rows.append(bytes(row))

raw = b"".join(b"\x00" + r for r in rows)


def chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


png = (
    b"\x89PNG\r\n\x1a\n"
    + chunk(b"IHDR", struct.pack(">IIBBBBB", SIZE, SIZE, 8, 2, 0, 0, 0))
    + chunk(b"IDAT", zlib.compress(raw, 9))
    + chunk(b"IEND", b"")
)

out = Path(__file__).with_name("icon.png")
out.write_bytes(png)
print(f"written: {out} ({len(png)} bytes)")
