"""Text in the scripts of India, in the PDF.

Inter and Fraunces draw Latin, Greek and Cyrillic. Everything else a trip plan
can hold — a destination typed in Devanagari, a temple OpenTripMap names in
Tamil, a monastery in Ladakh named in Tibetan, an Urdu street name — is drawn
in the Noto font of its script and shaped by HarfBuzz (`uharfbuzz`), the engine
browsers use: conjuncts joined, vowel signs placed and reordered, Arabic
letters joined. A script none of the fonts has (Chinese, Thai…) still prints
as boxes.

What the drawing code relies on:

- ReportLab shapes one word at a time, in the font of the word's first piece
  of text. So a word is never split between fonts: a word the main font cannot
  draw is drawn whole in a script font. Those carry ASCII and common
  punctuation, so "(केदारनाथ)" and "श्री-Krishna" each stay one word.
- HarfBuzz lays the letters of an Urdu word out right to left, but ReportLab
  puts the words of a line left to right — its own right-to-left support needs
  a package that is not on PyPI. So, as a browser does, lines are broken in
  reading order, and then each line is drawn with its right-to-left words
  reversed (ScriptParagraph, draw_text).

Fonts are registered with ReportLab the first time a word needs them.
"""

from __future__ import annotations

import re
import threading
import unicodedata
from collections.abc import Callable
from typing import TypeVar
from xml.sax.saxutils import escape

from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import ShapedStr, TTFont, shapeStr
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Paragraph

from app.pdf import theme
from app.pdf.formatting import clip

# The script fonts, by the Unicode blocks each script is written in (fonts/build_fonts.py builds them).
SCRIPT_BLOCKS: tuple[tuple[int, int, str], ...] = (
    (0x0600, 0x06FF, "NotoSansArabic"),
    (0x0750, 0x077F, "NotoSansArabic"),
    (0x08A0, 0x08FF, "NotoSansArabic"),
    (0x0900, 0x097F, "NotoSansDevanagari"),
    (0x0980, 0x09FF, "NotoSansBengali"),
    (0x0A00, 0x0A7F, "NotoSansGurmukhi"),
    (0x0A80, 0x0AFF, "NotoSansGujarati"),
    (0x0B00, 0x0B7F, "NotoSansOriya"),
    (0x0B80, 0x0BFF, "NotoSansTamil"),
    (0x0C00, 0x0C7F, "NotoSansTelugu"),
    (0x0C80, 0x0CFF, "NotoSansKannada"),
    (0x0D00, 0x0D7F, "NotoSansMalayalam"),
    (0x0F00, 0x0FFF, "NotoSerifTibetan"),
    (0x1C50, 0x1C7F, "NotoSansOlChiki"),
    (0x1CD0, 0x1CFF, "NotoSansDevanagari"),  # Vedic extensions
    (0xA8E0, 0xA8FF, "NotoSansDevanagari"),  # Devanagari extended
    (0xAAE0, 0xAAFF, "NotoSansMeeteiMayek"),
    (0xABC0, 0xABFF, "NotoSansMeeteiMayek"),
    (0xFB50, 0xFDFF, "NotoSansArabic"),  # presentation forms
    (0xFE70, 0xFEFF, "NotoSansArabic"),
)
RTL_FAMILIES = ("NotoSansArabic",)  # of those, the scripts written right to left

# What separates words: ReportLab's own list — Unicode white space and the zero-width space, but
# not the no-break space.
_SEPARATOR = re.compile("((?:[^\\S\u00a0]|\u200b)+)")

# Characters that only steer the shaping of their neighbours. They never decide a word's font, and
# Inter and Fraunces, which are not shaped, have no glyph for them: there they are left out.
_INVISIBLE = "\u200c\u200d\u2060\ufeff"

_registering = threading.Lock()
_registered: set[str] = set()


def register(name: str) -> None:
    """Make a font known to ReportLab by its name. The first call for a name loads the file."""
    if name in _registered:
        return
    with _registering:
        if name not in _registered:
            pdfmetrics.registerFont(TTFont(name, str(theme.font_path(name))))
            _registered.add(name)


def _glyphs(name: str) -> dict[int, int]:
    register(name)
    return pdfmetrics.getFont(name).face.charToGlyph


def script_family(character: str) -> str | None:
    code = ord(character)
    return next((family for start, end, family in SCRIPT_BLOCKS if start <= code <= end), None)


def _weight(font: str) -> str:
    """The weight of script font that goes with a main font: body text Regular, the rest Medium."""
    return "Regular" if font == theme.REGULAR else "Medium"


def _sans(font: str) -> str:
    """What draws a word the display face cannot (an arrow, Cyrillic): Inter, in a matching weight."""
    return theme.SEMIBOLD if font == theme.DISPLAY else font


def covers(font: str, text: str) -> bool:
    glyphs = _glyphs(font)
    return all(ord(character) in glyphs or character in _INVISIBLE for character in text)


def _coverage(font: str, text: str) -> int:
    glyphs = _glyphs(font)
    return sum(ord(character) in glyphs for character in text)


def word_font(word: str, font: str) -> str:
    """The one font a word is drawn in, when the text around it is set in `font`."""
    if covers(font, word):
        return font
    if covers(_sans(font), word):
        return _sans(font)
    families = dict.fromkeys(f for character in word if (f := script_family(character)))  # in order of first use
    candidates = [f"{family}-{_weight(font)}" for family in families]
    for candidate in candidates:
        if covers(candidate, word):
            return candidate
    # Nothing draws all of it (two scripts in one word, or a script there is no font for): the font
    # that draws most of it. What it lacks prints as boxes.
    return max([font, _sans(font), *candidates], key=lambda name: _coverage(name, word))


def is_shaped(font: str) -> bool:
    """A script font: its words go through HarfBuzz. Latin, Greek and Cyrillic are drawn as they are."""
    return font not in theme.FONTS


def _is_space(unit: str) -> bool:
    return bool(_SEPARATOR.fullmatch(unit))


def _placed(text: str, font: str) -> list[tuple[str, str]]:
    """(font, text) for each word and each space of `text`, in reading order."""
    placed: list[tuple[str, str]] = []
    for unit in _SEPARATOR.split(text):
        # a space goes with the word before it: it never needs a font of its own
        name = (placed[-1][0] if placed else font) if _is_space(unit) else word_font(unit, font)
        if not is_shaped(name):
            unit = unit.translate({ord(character): None for character in _INVISIBLE})
        if unit:
            placed.append((name, unit))
    return placed


def markup(text: str, font: str) -> tuple[str, bool]:
    """Paragraph markup for plain `text` set in `font`, and whether the paragraph must be shaped.

    Text from a plan is data, never markup: it is escaped. The words the font
    cannot draw are wrapped in a <font> tag naming the font that can.
    """
    parts: list[tuple[str, str]] = []  # (font, text), a run of words in one font each
    for name, unit in _placed(text, font):
        if parts and parts[-1][0] == name:
            parts[-1] = (name, parts[-1][1] + unit)
        else:
            parts.append((name, unit))
    xml = "".join(escape(run) if name == font else f'<font name="{name}">{escape(run)}</font>' for name, run in parts)
    return xml, any(is_shaped(name) for name, _ in parts)


def drawable(text: str, font: str) -> bool:
    """Whether every character of `text` is in the font it would be drawn in — none would print as a box."""
    return all(covers(name, unit) for name, unit in _placed(text, font))


# ── Right to left ──────────────────────────────────────────────────────────

Word = TypeVar("Word")


def direction(word: str) -> str:
    """ "rtl" for a word with Arabic letters, "ltr" for one with other letters, "neutral" for numbers and signs."""
    kinds = {unicodedata.bidirectional(character) for character in word}
    return "rtl" if kinds & {"R", "AL"} else "ltr" if "L" in kinds else "neutral"


def visual_order(words: list[Word], direction_of: Callable[[Word], str]) -> list[Word]:
    """The words of one line in the order they are drawn, from left to right.

    Each stretch of right-to-left words — and of the numbers and signs between
    them — is reversed. The rest of the line keeps its order.
    """
    ordered: list[Word] = []
    index = 0
    while index < len(words):
        if direction_of(words[index]) != "rtl":
            ordered.append(words[index])
            index += 1
            continue
        end = probe = index + 1
        while probe < len(words) and (kind := direction_of(words[probe])) != "ltr":
            probe += 1
            if kind == "rtl":
                end = probe
        ordered += reversed(words[index:end])
        index = end
    return ordered


def _in_rtl_font(fragment) -> bool:
    return getattr(fragment, "fontName", "").startswith(RTL_FAMILIES)


def _words_of(text: str) -> list[str]:
    """`text` split at its spaces. A slice of a shaped string keeps what HarfBuzz worked out for it."""
    words, start = [], 0
    for at, character in enumerate(f"{text} "):
        if character == " ":
            if at > start:
                words.append(text[start:at])
            start = at + 1
    return words


def _drawing_order(fragments: list) -> list:
    """The fragments of text of one laid-out line, a word each, in the order they are drawn."""
    words: list[tuple[object, str | None, str]] = []  # (fragment, word, direction)
    for fragment in fragments:
        text = getattr(fragment, "text", "")
        if hasattr(fragment, "cbDefn"):  # an image or an anchor holds its place
            words.append((fragment, None, "ltr"))
        elif not text.strip():
            words.append((fragment, None, "neutral"))  # nothing to draw: a line break
        else:
            # A shaped word's characters are glyphs, not letters: in an Arabic font, a word is right to left.
            rtl = _in_rtl_font(fragment)
            words += [(fragment, word, "rtl" if rtl else direction(word)) for word in _words_of(text)]

    ordered = visual_order(words, lambda word: word[2])
    drawn = []
    for at, (fragment, word, _) in enumerate(ordered):
        if word is None:
            drawn.append(fragment)
            continue
        more = any(later is not None for _, later, _ in ordered[at + 1 :])
        drawn.append(fragment.clone(text=word + " " if more else word))
    return drawn


class ScriptParagraph(Paragraph):
    """A paragraph whose right-to-left words are drawn right to left, line by line.

    ReportLab breaks the lines in reading order, as it does any paragraph; only
    the drawing reverses each line's right-to-left stretches. Splitting a
    paragraph across two pages works from the lines and never sees the change.
    """

    def draw(self) -> None:
        laid_out = self.blPara
        if laid_out.kind == 1 and any(_in_rtl_font(fragment) for line in laid_out.lines for fragment in line.words):
            self.blPara = laid_out.clone(
                lines=[line.clone(words=_drawing_order(line.words)) for line in laid_out.lines]
            )
        try:
            super().draw()
        finally:
            self.blPara = laid_out


def paragraph(xml: str, style: ParagraphStyle, shaped: bool) -> Paragraph:
    """A paragraph of markup from markup(). `shaped`: it has words HarfBuzz must shape."""
    if not shaped:
        return Paragraph(xml, style)
    return ScriptParagraph(xml, ParagraphStyle(f"{style.name}-shaped", parent=style, shaping=1))


# ── One line on the canvas ─────────────────────────────────────────────────


def _drawn(text: str, font: str, size: float) -> list[tuple[str, str, float]]:
    """(font, what to draw, its width) for each word of `text` and the space after it, in drawing order."""
    words = visual_order(
        [(name, unit) for name, unit in _placed(text, font) if not _is_space(unit)], lambda w: direction(w[1])
    )
    pieces = []
    for at, (name, word) in enumerate(words):
        if at:  # one space between words, in the font of the word before it
            space = words[at - 1][0]
            pieces.append((space, " ", pdfmetrics.stringWidth(" ", space, size)))
        piece: str = shapeStr(word, name, size) if is_shaped(name) else word
        if isinstance(piece, ShapedStr):
            width = sum(data.x_advance for data in piece.__shapeData__) * size / 1000
        else:
            width = pdfmetrics.stringWidth(piece, name, size)
        pieces.append((name, piece, width))
    return pieces


def text_width(text: str, font: str, size: float) -> float:
    """How wide `text` is when drawn with draw_text()."""
    return sum(width for _, _, width in _drawn(text, font, size))


def draw_text(canv: Canvas, x: float, y: float, text: str, font: str, size: float) -> float:
    """Draw `text` on one line from (x, y), in whatever scripts it is written in. Returns where it ends."""
    for name, piece, width in _drawn(text, font, size):
        canv.setFont(name, size)
        canv.drawString(x, y, piece)
        x += width
    return x


def shorten(text: str, limit: int) -> str:
    """`text` cut to `limit` characters with an ellipsis — between two syllables, never inside one."""
    return text if len(text) <= limit else f"{clip(text, limit - 1).rstrip()}…"
