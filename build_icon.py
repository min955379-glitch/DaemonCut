"""Generate the DaemonCut launcher icon (PNG + ICO) without external deps.

Produces:
  build_icon.py outputs icon.png and icon.ico in the repo root.
The ICO is embedded (base64) into the PowerShell setup script, so the icon
works on the user's machine even if Pillow is not installed there.
"""
import base64
import struct
import zlib

W, H = 256, 256

# Locked DaemonCut palette (matches backend UI_PALETTE / console design system).
OBSIDIAN = (18, 19, 22)      # #121316
BRASS = (214, 173, 85)       # #D6AD55
PAPER = (244, 239, 230)      # #F4EFE6
STEEL = (43, 91, 132)        # #2B5B84


def in_rounded_rect(x, y, x0, y0, x1, y1, radius):
    if x < x0 or x > x1 or y < y0 or y > y1:
        return False
    cx = min(max(x, x0 + radius), x1 - radius)
    cy = min(max(y, y0 + radius), y1 - radius)
    if (x < x0 + radius or x > x1 - radius) and (y < y0 + radius or y > y1 - radius):
        dx, dy = x - cx, y - cy
        return (dx * dx + dy * dy) <= radius * radius
    return True


def make_image():
    """Return a 256x256 RGB bytearray: obsidian rounded square, brass monitor,
    a play triangle in paper color, and a small steel activity dot."""
    px = bytearray(W * H * 3)
    for y in range(H):
        for x in range(W):
            i = (y * W + x) * 3
            px[i:i + 3] = bytes(OBSIDIAN)
    r = 56
    # Brass rounded-square badge.
    for y in range(H):
        for x in range(W):
            if in_rounded_rect(x, y, 20, 20, W - 20, H - 20, r):
                i = (y * W + x) * 3
                px[i:i + 3] = bytes(BRASS)
    # Monitor: rounded rect + stand, in obsidian for contrast on brass.
    mx0, my0, mx1, my1 = 58, 52, 198, 168
    mr = 22
    for y in range(my0, my1):
        for x in range(mx0, mx1):
            if in_rounded_rect(x, y, mx0, my0, mx1, my1, mr):
                i = (y * W + x) * 3
                px[i:i + 3] = bytes(OBSIDIAN)
    # Screen content: a play triangle (paper) + small steel bars.
    tri = [(136, 78), (136, 140), (196, 109)]
    for y in range(H):
        for x in range(W):
            # Barycentric check inside the triangle.
            (ax, ay), (bx, by), (cx, cy) = tri
            d1 = (x - bx) * (ay - by) - (ax - bx) * (y - by)
            d2 = (x - cx) * (by - cy) - (bx - cx) * (y - cy)
            d3 = (x - ax) * (cy - ay) - (cx - ax) * (y - ay)
            neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
            pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
            if not (neg and pos) and 58 <= x <= 198 and 52 <= y <= 168:
                i = (y * W + x) * 3
                px[i:i + 3] = bytes(PAPER)
    # Stand under the monitor.
    for y in range(170, 186):
        for x in range(108, 148):
            i = (y * W + x) * 3
            px[i:i + 3] = bytes(OBSIDIAN)
    for y in range(186, 194):
        for x in range(96, 160):
            i = (y * W + x) * 3
            px[i:i + 3] = bytes(OBSIDIAN)
    # Steel status dot in the top-right of the badge.
    for y in range(40, 74):
        for x in range(182, 216):
            dx, dy = x - 199, y - 57
            if dx * dx + dy * dy <= 16 * 16:
                i = (y * W + x) * 3
                px[i:i + 3] = bytes(STEEL)
    return px


def png_chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data)) + tag + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def write_png(path: str, px: bytearray) -> None:
    raw = b"".join(b"\x00" + bytes(px[y * W * 3:(y + 1) * W * 3]) for y in range(H))
    data = b"\x89PNG\r\n\x1a\n"
    data += png_chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
    data += png_chunk(b"IDAT", zlib.compress(raw, 9))
    data += png_chunk(b"IEND", b"")
    with open(path, "wb") as fh:
        fh.write(data)


def write_ico(path: str, png: bytes) -> None:
    # ICO container holding a single 256x256 PNG image.
    # Directory entry: width, height (256 -> stored as 0), palette, reserved,
    # color planes, bits-per-pixel, image size, image data offset (6+16=22).
    with open(path, "wb") as fh:
        fh.write(struct.pack("<HHH", 0, 1, 1))
        fh.write(struct.pack(
            "<BBBBHHI",
            0 if W >= 256 else W,
            0 if H >= 256 else H,
            0, 0, 1, 32, len(png),
        ))
        fh.write(struct.pack("<I", 6 + 16))
        fh.write(png)


if __name__ == "__main__":
    px = make_image()
    write_png("icon.png", px)
    with open("icon.png", "rb") as fh:
        png_bytes = fh.read()
    write_ico("icon.ico", png_bytes)
    b64 = base64.b64encode(png_bytes).decode("ascii")
    with open("icon.ico.base64.txt", "w", encoding="ascii") as fh:
        fh.write(b64)
    print("icon.png  :", len(png_bytes), "bytes")
    print("icon.ico  :", len(png_bytes) + 22, "bytes")
    print("base64     :", len(b64), "chars")
