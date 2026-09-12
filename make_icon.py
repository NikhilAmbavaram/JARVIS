"""make_icon.py - draws the Jarvis orb as ui/jarvis.ico, in the dashboard's own colours.

Only needed if the palette changes or you want a different look:  python .\make_icon.py
It writes ui/jarvis.ico (16-256 px, small sizes simplified) and a preview sheet in workspace/.
"""
from PIL import Image, ImageDraw, ImageFilter
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "ui"
PREVIEW = HERE / "workspace"
PREVIEW.mkdir(exist_ok=True)

ACCENT = (168, 85, 247)        # --accent
ACCENT_2 = (192, 132, 252)     # --accent-2
CORE_IN = (63, 31, 120)        # orb-core gradient start  #3f1f78
CORE_OUT = (26, 12, 50)        # orb-core gradient end    #1a0c32
BAR_TOP = (245, 232, 255)      # bars gradient top        #f5e8ff
BAR_SHAPE = [0.55, 0.82, 1.0, 0.82, 0.55]

SS = 8   # supersampling: draw big, shrink down, so the curves stay smooth


def mix(a, b, t):
    t = max(0.0, min(1.0, t))
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def ring(draw, c, radius, width, colour, alpha=255, start=None, end=None):
    box = [c - radius, c - radius, c + radius, c + radius]
    if start is None:
        draw.ellipse(box, outline=colour + (alpha,), width=int(width))
    else:
        draw.arc(box, start, end, fill=colour + (alpha,), width=int(width))


def draw_orb(size: int, detail: str) -> Image.Image:
    """detail: 'full' (rings, ticks, arc, 5 bars), 'mid' (ring, arc, 5 bars), 'small' (ring, 3 bars)."""
    n = size * SS
    c = n / 2
    half = n / 2
    image = Image.new("RGBA", (n, n), (0, 0, 0, 0))

    # a soft violet bloom, hugging the core the way .orb-glow does
    glow = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse([c - half * 0.44, c - half * 0.44, c + half * 0.44, c + half * 0.44], fill=ACCENT + (88,))
    image.alpha_composite(glow.filter(ImageFilter.GaussianBlur(n * 0.055)))

    draw = ImageDraw.Draw(image)
    stroke = n * (0.011 if detail == "full" else 0.017 if detail == "mid" else 0.028)

    if detail == "full":
        ring(draw, c, half * 0.96, stroke, ACCENT, 150)                          # outer hairline
        for i in range(46):                                                      # dashed tick ring
            a0 = i * (360 / 46)
            ring(draw, c, half * 0.855, stroke * 4.0, ACCENT, 120, a0, a0 + 3.6)
        ring(draw, c, half * 0.745, stroke * 2.2, ACCENT_2, 255, -62, 38)        # the sweeping arc
        ring(draw, c, half * 0.66, stroke * 0.9, ACCENT, 90)                     # mid ring
    elif detail == "mid":
        ring(draw, c, half * 0.95, stroke, ACCENT, 165)
        for i in range(24):
            a0 = i * 15
            ring(draw, c, half * 0.82, stroke * 3.4, ACCENT, 120, a0, a0 + 6.5)
        ring(draw, c, half * 0.82, stroke * 2.6, ACCENT_2, 255, -62, 22)
    else:
        ring(draw, c, half * 0.93, stroke, ACCENT_2, 225)

    # the core: concentric circles for the radial gradient, lit slightly from above
    core_r = half * (0.45 if detail == "full" else 0.50 if detail == "mid" else 0.64)
    steps = 110
    for i in range(steps, 0, -1):
        t = i / steps                       # 1 at the rim, 0 at the middle
        r = core_r * t
        lift = core_r * 0.12 * (1 - t)      # the highlight sits above centre, like the CSS gradient
        draw.ellipse([c - r, c - r - lift, c + r, c + r - lift], fill=mix(CORE_OUT, CORE_IN, (1 - t) ** 0.75) + (255,))
    draw.ellipse([c - core_r, c - core_r, c + core_r, c + core_r], outline=ACCENT_2 + (150,), width=max(1, int(stroke)))

    # the five level bars, drawn on their own layer so they can glow
    bars = BAR_SHAPE if detail != "small" else [0.74, 1.0, 0.74]
    layer = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    bar_draw = ImageDraw.Draw(layer)
    bar_w = core_r * (0.19 if detail != "small" else 0.28)
    gap = bar_w * (1.72 if detail != "small" else 1.9)
    span = bar_w + gap * (len(bars) - 1)
    x = c - span / 2
    tallest = core_r * 1.16
    for shape in bars:
        height = tallest * shape
        top, bottom = c - height / 2, c + height / 2
        slices = 40
        for i in range(slices):
            t = i / (slices - 1)
            y0 = top + (bottom - top) * t
            y1 = min(bottom, y0 + (bottom - top) / slices + 1)
            colour = mix(BAR_TOP, ACCENT_2, t / 0.55) if t < 0.55 else mix(ACCENT_2, ACCENT, (t - 0.55) / 0.45)
            bar_draw.rounded_rectangle([x, y0, x + bar_w, y1], radius=bar_w / 2, fill=colour + (255,))
        x += gap
    image.alpha_composite(layer.filter(ImageFilter.GaussianBlur(n * 0.012)))   # the bars' own glow
    image.alpha_composite(layer)

    return image.resize((size, size), Image.LANCZOS)


SIZES = [(256, "full"), (128, "full"), (64, "full"), (48, "mid"), (32, "mid"), (24, "small"), (16, "small")]
images = [draw_orb(size, detail) for size, detail in SIZES]
# preview: every size on the dashboard's background, then again on a light one
row = sum(size + 18 for size, _ in SIZES) + 20
sheet = Image.new("RGBA", (row, 300), (7, 4, 14, 255))
light = Image.new("RGBA", (row, 140), (243, 243, 247, 255))
x = 20
for image, (size, _) in zip(images, SIZES):
    sheet.alpha_composite(image, (x, 20 + (256 - size) // 2))
    light.alpha_composite(image, (x, 20 + (128 - min(size, 128)) // 2))
    x += size + 18
sheet.alpha_composite(light, (0, 160))
sheet.save(PREVIEW / "icon-preview.png")

images[0].save(OUT / "jarvis.ico", format="ICO", sizes=[(s, s) for s, _ in SIZES], append_images=images[1:])
print(f"Wrote {OUT / 'jarvis.ico'} ({(OUT / 'jarvis.ico').stat().st_size // 1024} KB)")
print("Sizes:", sorted(s for s, _ in SIZES))
print(f"Preview: {PREVIEW / 'icon-preview.png'}")
