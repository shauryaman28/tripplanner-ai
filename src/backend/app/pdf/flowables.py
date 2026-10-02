"""The pieces the PDF's pages are assembled from, drawn on a ReportLab canvas.

Marks (the map's pin, the logo, the budget's status icon) are plain functions;
the classes wrap them — and a few layouts ReportLab has no flowable for — so
they can sit in a story or a table cell.
"""

from __future__ import annotations

import io

from reportlab.lib.colors import HexColor, white
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Flowable

from app.pdf import theme
from app.pdf.plan import BudgetNote

for _name in theme.FONTS:
    pdfmetrics.registerFont(TTFont(_name, str(theme.font_path(_name))))

RADIUS = 8  # a card's corners
HAIRLINE = 0.6
DAY_BAR = 4.5  # the stripe of the day's colour down the left of its card
PIN = 14  # a pin beside a row, in points

INK_900, INK_600, INK_500, INK_400 = (HexColor(c) for c in (theme.INK_900, theme.INK_600, theme.INK_500, theme.INK_400))
INK_200, INK_100 = HexColor(theme.INK_200), HexColor(theme.INK_100)

STATUS = {"under": theme.GOOD, "tight": theme.WARN, "over": theme.BAD}  # (fill, text, soft tint)
_METER = {  # (fill, track) — the track is a lighter step of the fill
    "under": (theme.INK_900, theme.INK_200),
    "tight": (theme.WARN[0], "#fbe7b0"),
    "over": (theme.BAD[0], "#f6cfcf"),
}
# The fold of the paper plane: the site draws it in 35% ink over the gradient. As one flat
# colour here, so that the file has no transparency in it — some printers still mishandle that.
_LOGO_FOLD = "#a14d2d"


# ── Marks ──────────────────────────────────────────────────────────────────


def draw_pin(
    canv: Canvas, x: float, y: float, size: float, style: theme.PinStyle, label: str = "", icon: str = ""
) -> None:
    """The map's pin, centred on (x, y) — the same mark static_map.py draws on the picture."""
    canv.saveState()
    canv.translate(x, y)
    side = size * (theme.DIAMOND_SIDE if style.shape == "diamond" else 1.0)
    if style.shape == "diamond":
        canv.rotate(45)
    canv.setFillColor(HexColor(style.color))
    canv.roundRect(-side / 2, -side / 2, side, side, side * theme.PIN_CORNER[style.shape], stroke=0, fill=1)
    if style.shape == "diamond":
        canv.rotate(-45)

    if icon:
        _draw_icon(canv, size * 0.56, style, icon)
    elif label:
        font_size = size * 0.52
        canv.setFillColor(HexColor(style.ink))
        canv.setFont(theme.SEMIBOLD, font_size)
        canv.drawCentredString(0, -font_size * theme.CAP_HEIGHT / 2, label)
    canv.restoreState()


def _draw_icon(canv: Canvas, box: float, style: theme.PinStyle, icon: str) -> None:
    """The bed or the paper plane, from its 24 × 24 drawing, centred on the origin."""
    scale = box / theme.ICON_BOX

    def place(points: tuple[tuple[float, float], ...]) -> list[tuple[float, float]]:
        return [((px - theme.ICON_BOX / 2) * scale, (theme.ICON_BOX / 2 - py) * scale) for px, py in points]

    def path_through(points: list[tuple[float, float]]):
        path = canv.beginPath()
        path.moveTo(*points[0])
        for point in points[1:]:
            path.lineTo(*point)
        return path

    canv.setLineCap(1)
    canv.setLineJoin(1)
    if icon == "bed":
        canv.setStrokeColor(HexColor(style.ink))
        canv.setLineWidth(theme.BED_STROKE_WIDTH * scale)
        for line in theme.BED_STROKES:
            canv.drawPath(path_through(place(line)), stroke=1, fill=0)
    else:
        outline = path_through(place(theme.PLANE_OUTLINE))
        outline.close()
        canv.setFillColor(HexColor(style.ink))
        canv.drawPath(outline, stroke=0, fill=1)
        canv.setStrokeColor(HexColor(style.color))
        canv.setLineWidth(1.3 * scale)
        canv.drawPath(path_through(place(theme.PLANE_FOLD)), stroke=1, fill=0)


def draw_logo(canv: Canvas, x: float, y: float, size: float) -> None:
    """The site's mark (components/Brand.tsx): a paper plane in the sunset gradient on an ink tile."""
    icon = size * 0.58
    scale = icon / theme.ICON_BOX
    left, bottom = x + (size - icon) / 2, y + (size - icon) / 2

    def at(px: float, py: float) -> tuple[float, float]:
        return left + px * scale, bottom + (theme.ICON_BOX - py) * scale

    canv.saveState()
    canv.setFillColor(INK_900)
    canv.roundRect(x, y, size, size, size * 0.28, stroke=0, fill=1)

    plane = canv.beginPath()
    plane.moveTo(*at(*theme.PLANE_OUTLINE[0]))
    for point in theme.PLANE_OUTLINE[1:]:
        plane.lineTo(*at(*point))
    plane.close()
    canv.saveState()
    canv.clipPath(plane, stroke=0, fill=0)
    canv.linearGradient(*at(3, 21), *at(21, 3), [HexColor(c) for c in theme.SUNSET], positions=[0, 0.55, 1])
    canv.restoreState()

    canv.setStrokeColor(HexColor(_LOGO_FOLD))
    canv.setLineWidth(1.4 * scale)
    canv.setLineCap(1)
    canv.line(*at(*theme.PLANE_FOLD[0]), *at(*theme.PLANE_FOLD[1]))
    canv.restoreState()


def _draw_status_icon(canv: Canvas, x: float, y: float, size: float, state: str) -> None:
    """A status is an icon plus words, never colour alone: a tick, a warning triangle, or an alert."""
    canv.saveState()
    canv.translate(x, y)
    canv.setLineWidth(size * 0.11)
    canv.setLineCap(1)
    canv.setLineJoin(1)
    half = size / 2
    if state == "tight":
        triangle = canv.beginPath()
        triangle.moveTo(0, half * 0.95)
        triangle.lineTo(half, -half * 0.8)
        triangle.lineTo(-half, -half * 0.8)
        triangle.close()
        canv.drawPath(triangle, stroke=1, fill=0)
    else:
        canv.circle(0, 0, half, stroke=1, fill=0)

    if state == "under":
        tick = canv.beginPath()
        tick.moveTo(-half * 0.42, 0)
        tick.lineTo(-half * 0.1, -half * 0.32)
        tick.lineTo(half * 0.45, half * 0.32)
        canv.drawPath(tick, stroke=1, fill=0)
    else:  # an exclamation mark: a stroke and, with the round line cap, a dot
        lift = -half * 0.12 if state == "tight" else 0
        canv.line(0, half * 0.42 + lift, 0, -half * 0.08 + lift)
        canv.line(0, -half * 0.44 + lift, 0, -half * 0.45 + lift)
    canv.restoreState()


# ── Flowables ──────────────────────────────────────────────────────────────


class PinMark(Flowable):
    """A pin beside a row or in the map's key: a day's numbered stop, the hotel, the airport."""

    def __init__(self, style: theme.PinStyle, label: str = "", icon: str = "", size: float = PIN, drop: float = 1.5):
        super().__init__()
        self.style, self.label, self.icon, self.size, self.drop = style, label, icon, size, drop

    def wrap(self, _width: float, _height: float) -> tuple[float, float]:
        return self.size, self.size + self.drop  # `drop` lowers it onto the first line of the words beside it

    def draw(self) -> None:
        draw_pin(self.canv, self.size / 2, self.size / 2, self.size, self.style, self.label, self.icon)


class Card(Flowable):
    """A white box with rounded corners and a hairline edge around another flowable.

    `bar` paints the left edge in a colour — the day's, on a day card. It is
    drawn inside the rounded outline, so it follows the corners. A card is
    never split across two pages.
    """

    def __init__(self, content: Flowable, width: float, bar: str | None = None):
        super().__init__()
        self.content, self.width, self.bar = content, width, bar
        self.inset = DAY_BAR if bar else 0.0
        self.height = 0.0

    def wrap(self, _width: float, available_height: float) -> tuple[float, float]:
        _, self.height = self.content.wrapOn(self.canv, self.width - self.inset, available_height)
        return self.width, self.height

    def draw(self) -> None:
        canv = self.canv
        outline = canv.beginPath()
        outline.roundRect(0, 0, self.width, self.height, RADIUS)
        canv.saveState()
        canv.clipPath(outline, stroke=0, fill=0)
        canv.setFillColor(white)
        canv.rect(0, 0, self.width, self.height, stroke=0, fill=1)
        if self.bar:
            canv.setFillColor(HexColor(self.bar))
            canv.rect(0, 0, self.inset, self.height, stroke=0, fill=1)
        canv.restoreState()

        self.content.drawOn(canv, self.inset, 0)

        canv.setStrokeColor(INK_200)
        canv.setLineWidth(HAIRLINE)
        canv.roundRect(0, 0, self.width, self.height, RADIUS, stroke=1, fill=0)


class Brand(Flowable):
    """The logo and the wordmark."""

    def __init__(self, size: float = 20):
        super().__init__()
        self.size = size

    def wrap(self, width: float, _height: float) -> tuple[float, float]:
        return width, self.size

    def draw(self) -> None:
        draw_logo(self.canv, 0, 0, self.size)
        self.canv.setFillColor(INK_900)
        self.canv.setFont(theme.DISPLAY, self.size * 0.7)
        self.canv.drawString(self.size + 7, self.size * 0.27, "TripPlanner")


class Chips(Flowable):
    """Short labels in rounded chips, wrapping onto as many lines as they need."""

    FONT, SIZE, PAD_X, HEIGHT, GAP = theme.MEDIUM, 7.5, 6.5, 15.0, 4.5

    def __init__(self, labels: tuple[str, ...]):
        super().__init__()
        self.labels = labels
        self.placed: list[tuple[float, int, float, str]] = []
        self.height = 0.0

    def wrap(self, width: float, _height: float) -> tuple[float, float]:
        self.placed, x, line = [], 0.0, 0
        for label in self.labels:
            chip = min(pdfmetrics.stringWidth(label, self.FONT, self.SIZE) + 2 * self.PAD_X, width)
            if x and x + chip > width:
                x, line = 0.0, line + 1
            self.placed.append((x, line, chip, label))
            x += chip + self.GAP
        self.height = (line + 1) * self.HEIGHT + line * self.GAP if self.labels else 0.0
        return width, self.height

    def draw(self) -> None:
        canv = self.canv
        for x, line, width, label in self.placed:
            y = self.height - (line + 1) * self.HEIGHT - line * self.GAP
            canv.setFillColor(INK_100)
            canv.roundRect(x, y, width, self.HEIGHT, self.HEIGHT / 2, stroke=0, fill=1)
            canv.setFillColor(INK_600)
            canv.setFont(self.FONT, self.SIZE)
            canv.saveState()
            clip = canv.beginPath()
            clip.rect(x + self.PAD_X, y, width - 2 * self.PAD_X, self.HEIGHT)
            canv.clipPath(clip, stroke=0, fill=0)  # a label longer than the line is cut, not spilled
            canv.drawString(x + self.PAD_X, y + (self.HEIGHT - self.SIZE * theme.CAP_HEIGHT) / 2, label)
            canv.restoreState()


class BudgetPill(Flowable):
    """ "₹32,800 under budget" on its soft status colour, with the status icon — set to the right of its cell."""

    FONT, SIZE, HEIGHT, PAD_X, ICON = theme.MEDIUM, 8, 18.0, 8.0, 8.5

    def __init__(self, note: BudgetNote):
        super().__init__()
        self.note = note
        self.available = 0.0

    def wrap(self, width: float, _height: float) -> tuple[float, float]:
        self.available = width
        return width, self.HEIGHT

    def draw(self) -> None:
        _, ink, soft = STATUS[self.note.state]
        canv = self.canv
        width = pdfmetrics.stringWidth(self.note.text, self.FONT, self.SIZE) + self.ICON + 4 + 2 * self.PAD_X
        x = self.available - width
        canv.setFillColor(HexColor(soft))
        canv.roundRect(x, 0, width, self.HEIGHT, self.HEIGHT / 2, stroke=0, fill=1)
        canv.setFillColor(HexColor(ink))
        canv.setStrokeColor(HexColor(ink))
        _draw_status_icon(canv, x + self.PAD_X + self.ICON / 2, self.HEIGHT / 2, self.ICON, self.note.state)
        canv.setFont(self.FONT, self.SIZE)
        baseline = (self.HEIGHT - self.SIZE * theme.CAP_HEIGHT) / 2
        canv.drawString(x + self.PAD_X + self.ICON + 4, baseline, self.note.text)


class Meter(Flowable):
    """How much of the budget the total uses."""

    HEIGHT = 6.0

    def __init__(self, note: BudgetNote):
        super().__init__()
        self.note = note
        self.width = 0.0

    def wrap(self, width: float, _height: float) -> tuple[float, float]:
        self.width = width
        return width, self.HEIGHT

    def draw(self) -> None:
        fill, track = _METER[self.note.state]
        canv = self.canv
        outline = canv.beginPath()
        outline.roundRect(0, 0, self.width, self.HEIGHT, self.HEIGHT / 2)
        canv.saveState()
        canv.clipPath(outline, stroke=0, fill=0)
        canv.setFillColor(HexColor(track))
        canv.rect(0, 0, self.width, self.HEIGHT, stroke=0, fill=1)
        canv.setFillColor(HexColor(fill))
        canv.rect(0, 0, self.width * min(self.note.used, 1.0), self.HEIGHT, stroke=0, fill=1)
        canv.restoreState()


class Legend(Flowable):
    """What each mark on the map means: one entry per day with a pin, then the hotel and the airport."""

    FONT, SIZE, MARK, GAP, LINE = theme.MEDIUM, 8, 10.0, 12.0, 15.0

    def __init__(self, entries: list[tuple[theme.PinStyle, str, str]]):
        super().__init__()
        self.entries = entries  # (style, icon, words)
        self.placed: list[tuple[float, int, theme.PinStyle, str, str]] = []
        self.height = 0.0

    def wrap(self, width: float, _height: float) -> tuple[float, float]:
        self.placed, x, line = [], 0.0, 0
        for style, icon, words in self.entries:
            entry = self.MARK + 4 + pdfmetrics.stringWidth(words, self.FONT, self.SIZE)
            if x and x + entry > width:
                x, line = 0.0, line + 1
            self.placed.append((x, line, style, icon, words))
            x += entry + self.GAP
        self.height = (line + 1) * self.LINE if self.entries else 0.0
        return width, self.height

    def draw(self) -> None:
        canv = self.canv
        for x, line, style, icon, words in self.placed:
            y = self.height - (line + 0.5) * self.LINE
            draw_pin(canv, x + self.MARK / 2, y, self.MARK, style, icon=icon)
            canv.setFillColor(INK_600)
            canv.setFont(self.FONT, self.SIZE)
            canv.drawString(x + self.MARK + 4, y - self.SIZE * theme.CAP_HEIGHT / 2, words)


class MapPicture(Flowable):
    """The static map, with a card's rounded corners and hairline edge."""

    def __init__(self, image: bytes, width: float):
        super().__init__()
        self.reader = ImageReader(io.BytesIO(image))
        pixels_wide, pixels_high = self.reader.getSize()
        self.width, self.height = width, width * pixels_high / pixels_wide

    def wrap(self, _width: float, _height: float) -> tuple[float, float]:
        return self.width, self.height

    def draw(self) -> None:
        canv = self.canv
        outline = canv.beginPath()
        outline.roundRect(0, 0, self.width, self.height, RADIUS)
        canv.saveState()
        canv.clipPath(outline, stroke=0, fill=0)
        canv.drawImage(self.reader, 0, 0, self.width, self.height)
        canv.restoreState()
        canv.setStrokeColor(INK_200)
        canv.setLineWidth(HAIRLINE)
        canv.roundRect(0, 0, self.width, self.height, RADIUS, stroke=1, fill=0)


class Bookmark(Flowable):
    """An entry in the PDF's outline, pointing at the place it sits in the story."""

    def __init__(self, title: str):
        super().__init__()
        self.title = title

    def wrap(self, _width: float, _height: float) -> tuple[float, float]:
        return 0, 0

    def draw(self) -> None:
        key = f"section-{self.title}"
        self.canv.bookmarkPage(key)
        self.canv.addOutlineEntry(self.title, key, level=0)
