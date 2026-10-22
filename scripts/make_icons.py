"""Generate ScholarLens icons: transparent magnifier, no base plate."""
import os

from PIL import Image, ImageDraw

_DEFAULT_OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")
OUT_DIR = os.environ.get("SCHOLARLENS_ICON_OUT") or _DEFAULT_OUT
os.makedirs(OUT_DIR, exist_ok=True)

ACCENT = (108, 140, 255, 255)
GLASS = (108, 140, 255, 60)
SHINE = (230, 232, 238, 230)


def make_icon(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img, "RGBA")
    s = size / 16.0
    cx = cy = size * 0.44
    r_outer = 4.6 * s
    d.ellipse([cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer],
              fill=GLASS, outline=ACCENT, width=max(2, int(1.9 * s)))
    hx1, hy1 = cx + 3.3 * s, cy + 3.3 * s
    hx2, hy2 = cx + 6.8 * s, cy + 6.8 * s
    d.line([hx1, hy1, hx2, hy2], fill=ACCENT, width=max(2, int(2.4 * s)))
    hr = 1.2 * s
    d.ellipse([hx2 - hr, hy2 - hr, hx2 + hr, hy2 + hr], fill=ACCENT)
    d.ellipse([cx - 2.6 * s, cy - 2.8 * s, cx - 0.6 * s, cy - 0.8 * s], fill=SHINE)
    return img


sizes = [16, 24, 32, 48, 64, 128, 256]
imgs = {s: make_icon(s) for s in sizes}
imgs[256].save(os.path.join(OUT_DIR, "icon.png"))
imgs[256].save(os.path.join(OUT_DIR, "icon-256.png"))
imgs[256].save(os.path.join(OUT_DIR, "icon.ico"),
               sizes=[(s, s) for s in sizes], bitmap_format="bmp")
print("written:", sorted(os.listdir(OUT_DIR)))
