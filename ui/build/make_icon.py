"""Draws icon.png (1024x1024): stacked profile cards on a graphite squircle.
Pure python: signed-distance rounded rects/circles with 1px analytic AA."""
import math, struct, zlib

S = 1024
img = [[(0, 0, 0, 0)] * S for _ in range(S)]

def hexc(h): return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))

def sd_rrect(px, py, x0, y0, x1, y1, r):
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    hx, hy = (x1 - x0) / 2 - r, (y1 - y0) / 2 - r
    qx, qy = abs(px - cx) - hx, abs(py - cy) - hy
    return math.hypot(max(qx, 0), max(qy, 0)) + min(max(qx, qy), 0) - r

def sd_circle(px, py, cx, cy, r): return math.hypot(px - cx, py - cy) - r

def paint(sdf, bbox, color, alpha=1.0):
    c = hexc(color)
    x0, y0, x1, y1 = (int(v) for v in bbox)
    for y in range(max(0, y0 - 2), min(S, y1 + 2)):
        row = img[y]
        for x in range(max(0, x0 - 2), min(S, x1 + 2)):
            a = min(1.0, max(0.0, 0.5 - sdf(x + 0.5, y + 0.5))) * alpha
            if a <= 0: continue
            r, g, b, da = row[x]
            da /= 255
            na = a + da * (1 - a)
            row[x] = tuple(round((c[i] * a + (r, g, b)[i] * da * (1 - a)) / na) if na else 0 for i in range(3)) + (round(na * 255),)

def rrect(x0, y0, x1, y1, r, color, alpha=1.0):
    paint(lambda px, py: sd_rrect(px, py, x0, y0, x1, y1, r), (x0, y0, x1, y1), color, alpha)

def circle(cx, cy, r, color):
    paint(lambda px, py: sd_circle(px, py, cx, cy, r), (cx - r, cy - r, cx + r, cy + r), color)

rrect(100, 100, 924, 924, 186, "#22262d")             # macOS grid squircle-ish
rrect(262, 262, 662, 612, 44, "#3a414c")              # back card
rrect(322, 322, 722, 672, 44, "#566070")              # middle card
rrect(382, 382, 782, 732, 44, "#eef1f5")              # front card
circle(474, 482, 48, "#3b8cf0")                       # avatar
rrect(548, 452, 722, 476, 12, "#aab3bf")              # name line
rrect(548, 494, 676, 516, 11, "#c9d0d8")              # sub line
rrect(432, 588, 732, 610, 11, "#c9d0d8")
rrect(432, 640, 660, 662, 11, "#c9d0d8")

raw = b"".join(b"\0" + b"".join(struct.pack("4B", *p) for p in row) for row in img)
def chunk(t, d): return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
open("icon.png", "wb").write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", S, S, 8, 6, 0, 0, 0))
                             + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
print("icon.png written")
