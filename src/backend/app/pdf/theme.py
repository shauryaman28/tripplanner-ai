"""How the PDF looks: the site's colours, day palette, pin marks and typefaces.

The values are the frontend's own (tailwind.config.ts, lib/map.ts), so a day is
the same colour on the trip page, on the web map and on paper. Nothing here
draws — document.py (ReportLab) and static_map.py (Pillow) both read from it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# ── Colours (tailwind.config.ts) ───────────────────────────────────────────

INK_900 = "#0b0b0b"
INK_800 = "#22211f"
INK_700 = "#3a3936"
INK_600 = "#52514e"
INK_500 = "#6f6d68"
INK_400 = "#898781"
INK_200 = "#e1e0d9"
INK_100 = "#f0efec"
WHITE = "#ffffff"

# Status colours always come with words: (fill, text on its soft tint, soft tint).
GOOD = ("#0ca30c", "#0b5d0b", "#ecf8ec")
WARN = ("#fab219", "#7a5200", "#fff5d9")
BAD = ("#d03b3b", "#9f2424", "#fdeeee")

SUNSET = ("#f6b73c", "#f2703f", "#e0457b")  # the logo's gradient, at 0 / 0.55 / 1

# ── Pins (lib/map.ts) ──────────────────────────────────────────────────────

# One colour per day, validated for every pair (DECISIONS #81). Past the last one the hues come
# round again with a different shape.
DAY_COLORS = ("#2a78d6", "#1baf7a", "#882255", "#ee7733", "#117733", "#56b4e9", "#4a3aa7")
PIN_SHAPES = ("circle", "square", "diamond")
_LIGHT_FILLS = {"#1baf7a", "#ee7733", "#56b4e9"}  # white text fails contrast on these


@dataclass(frozen=True)
class PinStyle:
    color: str
    ink: str  # colour of the label drawn on the fill
    shape: str


def day_style(day: int) -> PinStyle:
    index = max(int(day or 1), 1) - 1
    color = DAY_COLORS[index % len(DAY_COLORS)]
    return PinStyle(
        color=color,
        ink=INK_900 if color in _LIGHT_FILLS else WHITE,
        shape=PIN_SHAPES[(index // len(DAY_COLORS)) % len(PIN_SHAPES)],
    )


HOTEL_STYLE = PinStyle(color="#f4c430", ink=INK_900, shape="square")
AIRPORT_STYLE = PinStyle(color=INK_700, ink=WHITE, shape="circle")
UNMAPPED_STYLE = PinStyle(color=INK_200, ink=INK_500, shape="circle")  # a stop the map cannot place

# How far each shape's corners are rounded, as a share of its side; a diamond is a smaller
# square turned 45°, so that it takes up about the room a circle does.
PIN_CORNER = {"circle": 0.5, "square": 0.3, "diamond": 0.24}
DIAMOND_SIDE = 0.8

# Icons inside a pin, as lines in a 24 × 24 box (y down), so both renderers draw the same thing.
# The bed is the icon the web map's hotel pin wears; the paper plane is the logo.
ICON_BOX = 24.0
BED_STROKES: tuple[tuple[tuple[float, float], ...], ...] = (
    ((2, 20), (2, 12), (2.59, 10.59), (4, 10), (20, 10), (21.41, 10.59), (22, 12), (22, 20)),
    ((4, 10), (4, 6), (4.59, 4.59), (6, 4), (18, 4), (19.41, 4.59), (20, 6), (20, 10)),
    ((12, 4), (12, 10)),
    ((2, 18), (22, 18)),
)
BED_STROKE_WIDTH = 2.2
PLANE_OUTLINE: tuple[tuple[float, float], ...] = ((21, 3), (3, 10.2), (9.9, 13.1), (12.8, 20))
PLANE_FOLD: tuple[tuple[float, float], ...] = ((9.9, 13.1), (21, 3))

# ── Typefaces ──────────────────────────────────────────────────────────────

FONT_DIR = Path(__file__).parent / "fonts"
SCRIPT_FONT_DIR = FONT_DIR / "noto"  # the other scripts of India (Phase 20) — scripts.py picks among them

REGULAR = "Inter-Regular"
MEDIUM = "Inter-Medium"
SEMIBOLD = "Inter-SemiBold"
DISPLAY = "Fraunces-Medium"  # headings only, as on the site
FONTS = (REGULAR, MEDIUM, SEMIBOLD, DISPLAY)

# Inter's capital height, as a share of the font size — to centre a number inside a pin.
CAP_HEIGHT = 0.727


def font_path(name: str) -> Path:
    return (FONT_DIR if name in FONTS else SCRIPT_FONT_DIR) / f"{name}.ttf"
