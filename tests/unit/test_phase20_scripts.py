"""Phase 20 — text in the scripts of India, in the PDF.

  fonts          every script has its Noto font in two weights, built for the blocks scripts.py picks by
  choosing       a word is drawn whole in one font; plan text is escaped and only what the font lacks is tagged
  shaping        HarfBuzz is installed and joins what it should
  cutting        a name is cut between two syllables, never inside one
  right to left  Urdu is drawn right to left, line by line — in a paragraph, split or not, and on one line
  document       every part of the PDF draws the scripts: cover, chips, running head, day cards, ledger, map key
  download       the file is named in the destination's own script

The PDF is built for real and read back with pypdf. Where the drawing order
matters, ReportLab's own line drawing is watched.
"""

import ast
import io
import unicodedata
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from PIL import Image
from pypdf import PdfReader
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import ShapedStr, shapeStr
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Paragraph
from reportlab.platypus import paragraph as reportlab_paragraph
from reportlab.platypus.paragraph import _getFragWords

from app.pdf import export_itinerary, scripts, theme
from app.pdf.document import build_pdf
from app.pdf.flowables import Chips
from app.pdf.formatting import clip
from app.pdf.scripts import SCRIPT_BLOCKS
from tests.fakes import fake_tile, flight
from tests.unit.test_phase19_pdf import START, _client, _itinerary, _pages, _plan, _trip

BODY = ParagraphStyle("body", fontName=theme.REGULAR, fontSize=10, leading=13.5)


@pytest.fixture(scope="module")
def map_picture() -> bytes:
    """A stand-in for the static map: any JPEG will do for laying the page out."""
    out = io.BytesIO()
    Image.new("RGB", (1360, 760), "#dfe6dc").save(out, "JPEG")
    return out.getvalue()


@pytest.fixture
def tiles():
    with patch("app.pdf.static_map._download_tile", AsyncMock(side_effect=fake_tile)) as download:
        yield download


# A word in each script, as a place in a plan might be named.
SAMPLES = {
    "NotoSansDevanagari": "केदारनाथ",
    "NotoSansBengali": "দক্ষিণেশ্বর",
    "NotoSansGurmukhi": "ਅੰਮ੍ਰਿਤਸਰ",
    "NotoSansGujarati": "દ્વારકા",
    "NotoSansOriya": "ଭୁବନେଶ୍ୱର",
    "NotoSansTamil": "மீனாட்சி",
    "NotoSansTelugu": "తిరుమల",
    "NotoSansKannada": "ಮೈಸೂರು",
    "NotoSansMalayalam": "തിരുവനന്തപുരം",
    "NotoSansArabic": "سرینگر",
    "NotoSerifTibetan": "ཐིག་སེ",
    "NotoSansOlChiki": "ᱥᱟᱱᱛᱟᱲᱤ",
    "NotoSansMeeteiMayek": "ꯃꯅꯤꯄꯨꯔ",
}


# ── Fonts ──────────────────────────────────────────────────────────────────


def test_every_script_has_a_sample():
    assert set(SAMPLES) == {family for _, _, family in SCRIPT_BLOCKS}


@pytest.mark.parametrize("weight", ["Regular", "Medium"])
@pytest.mark.parametrize(("family", "word"), SAMPLES.items())
def test_each_script_is_drawn_in_its_own_font_with_what_words_need_besides(family, word, weight):
    font = f"{family}-{weight}"
    assert theme.font_path(font).is_file()
    assert scripts.word_font(word, theme.REGULAR if weight == "Regular" else theme.MEDIUM) == font
    # ASCII keeps "(केदारनाथ)" or "श्री-Krishna" one word, and a no-break space may join two
    assert scripts.covers(font, "".join(map(chr, range(0x20, 0x7F))) + "\u00a0")
    # a script with combining signs needs the dotted circle HarfBuzz sets a stray one on
    signs = any(
        unicodedata.category(chr(code)).startswith("M")
        for start, end, block_family in SCRIPT_BLOCKS
        if block_family == family
        for code in range(start, end + 1)
    )
    assert scripts.covers(font, "\u25cc") or not signs


def test_the_fonts_are_picked_by_the_blocks_they_were_built_for():
    """fonts/build_fonts.py and scripts.py name the same Unicode blocks for each script."""
    tree = ast.parse((theme.FONT_DIR / "build_fonts.py").read_text())
    built_for = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "SCRIPTS"
    )
    blocks = {
        (int(start, 16), int(end, 16), family)
        for family, ranges in built_for.items()
        for start, end in (block.removeprefix("U+").split("-") for block in ranges.split(","))
    }
    assert blocks == set(SCRIPT_BLOCKS)


def test_a_latin_plan_embeds_only_the_four_faces_it_always_did():
    reader = PdfReader(io.BytesIO(build_pdf(_plan(), None)))
    embedded = {
        str(font["/BaseFont"]).split("+")[-1]
        for page in reader.pages
        for font in (ref.get_object() for ref in page["/Resources"]["/Font"].values())
        if "/FontFile2" in font.get("/FontDescriptor", {})
    }
    assert embedded == set(theme.FONTS)


# ── Choosing a font ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("word", "font", "drawn_in"),
    [
        ("Goa", theme.REGULAR, theme.REGULAR),
        ("São", theme.DISPLAY, theme.DISPLAY),
        ("Москва", theme.REGULAR, theme.REGULAR),  # Inter has Cyrillic
        ("Москва", theme.DISPLAY, theme.SEMIBOLD),  # Fraunces has not: Inter, in a heading's weight
        ("→", theme.DISPLAY, theme.SEMIBOLD),
        ("केदारनाथ", theme.REGULAR, "NotoSansDevanagari-Regular"),
        ("केदारनाथ", theme.SEMIBOLD, "NotoSansDevanagari-Medium"),
        ("केदारनाथ", theme.DISPLAY, "NotoSansDevanagari-Medium"),  # a heading: Medium, as Fraunces is
        ("(केदारनाथ),", theme.REGULAR, "NotoSansDevanagari-Regular"),  # punctuation goes with its word
        ("श्री-Krishna", theme.REGULAR, "NotoSansDevanagari-Regular"),  # one word, one font
        ("দক্ষিণেশ্বর।", theme.REGULAR, "NotoSansBengali-Regular"),  # the danda is Devanagari's, and every Indic font's
        ("₹500", theme.REGULAR, theme.REGULAR),
    ],
)
def test_a_word_is_drawn_whole_in_one_font(word, font, drawn_in):
    assert scripts.word_font(word, font) == drawn_in


def test_a_word_no_font_can_draw_prints_in_the_one_that_draws_most_of_it():
    assert scripts.word_font("北京", theme.REGULAR) == theme.REGULAR  # boxes, as before Phase 20
    assert not scripts.drawable("北京 trip", theme.REGULAR)
    assert scripts.drawable("Goa गोवा சென்னை سرینگر", theme.REGULAR)


def test_plan_text_is_escaped_and_only_what_the_font_lacks_is_tagged():
    xml, shaped = scripts.markup("Fort <b>&</b> केदारनाथ मंदिर, Goa", theme.REGULAR)
    assert xml == 'Fort &lt;b&gt;&amp;&lt;/b&gt; <font name="NotoSansDevanagari-Regular">केदारनाथ मंदिर, </font>Goa'
    assert shaped  # a paragraph with words in a script font is shaped by HarfBuzz…
    assert scripts.markup("Fort Aguada", theme.REGULAR) == ("Fort Aguada", False)  # …and one without is not


@pytest.mark.parametrize(
    "text",
    [
        "Kedarnath Temple (केदारनाथ मंदिर) and श्री-Krishna गुफा",
        "मीनाक्षी மீனாட்சி அம்மன் கோயில் — Madurai",
        "گلی 12 لال چوک, Srinagar",
        "ཐིག་སེ་དགོན་པ། Thiksey  Monastery",
        "Hampi\u00a0ಹಂಪಿ ಮೈಸೂರು",
    ],
)
def test_reportlab_never_sees_a_word_in_two_fonts(text):
    """ReportLab shapes a word in the font of its first piece: a word in two fonts would come out in one."""
    xml, shaped = scripts.markup(text, theme.REGULAR)
    for word in _getFragWords(scripts.paragraph(xml, BODY, shaped).frags):
        assert len({fragment.fontName for fragment, piece in word[1:] if piece}) == 1, word


def test_joiners_stay_in_shaped_words_and_leave_latin_ones():
    """Inter has no glyph for a zero-width joiner: unshaped, it would print as a box."""
    assert scripts.markup("Taj\u200dMahal\ufeff", theme.REGULAR) == ("TajMahal", False)
    xml, _ = scripts.markup("क्\u200dष", theme.REGULAR)
    assert "\u200d" in xml


# ── Shaping ────────────────────────────────────────────────────────────────


def test_harfbuzz_is_installed_and_joins_the_letters():
    """Without uharfbuzz ReportLab silently draws the letters unjoined (requirements.txt: reportlab[shaping])."""
    assert pdfmetrics.getFont(theme.REGULAR).shapable
    for font in ("NotoSansDevanagari-Regular", "NotoSansArabic-Regular"):
        scripts.register(font)
    assert len(shapeStr("क्ष", "NotoSansDevanagari-Regular", 10)) == 1  # क + virama + ष: one conjunct
    assert isinstance(shapeStr("مسجد", "NotoSansArabic-Regular", 10), ShapedStr)  # joined letters


def test_a_paragraph_with_words_in_a_script_is_shaped():
    """Unshaped, क्षत्रिय would print as its letters side by side, a virama showing: no conjunct."""
    xml, shaped = scripts.markup("Kshatriya क्षत्रिय", theme.REGULAR)
    paragraph = scripts.paragraph(xml, BODY, shaped)
    paragraph.wrap(400, 100)
    (line,) = paragraph.blPara.lines
    word = str(line.words[-1].text).strip()
    assert word == str(shapeStr("क्षत्रिय", "NotoSansDevanagari-Regular", 10)) and len(word) < len("क्षत्रिय")


# ── Cutting ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "केदारनाथ मंदिर",
        "क्षत्रिय क्\u200dष",
        "சென்னை மீனாட்சி",
        "ಮೈಸೂರು ಅರಮನೆ",
        "ཐིག་སེ་དགོན་པ།",
        "سُرینگر کی جامع مسجد",
        "Fort Aguada",
    ],
)
def test_a_name_is_cut_between_syllables(text):
    for limit in range(1, len(text)):
        kept = clip(text, limit)
        rest = text[len(kept) :]
        assert len(kept) <= limit and text.startswith(kept)
        assert not unicodedata.category(rest[0]).startswith("M")  # no sign left without its letter
        assert rest[0] not in "\u200c\u200d" and (not kept or kept[-1] not in "\u200c\u200d")
        assert not kept or unicodedata.combining(kept[-1]) != 9  # no virama left hanging


def test_shorten_adds_an_ellipsis_to_what_it_cuts():
    assert scripts.shorten("Fort Aguada", 11) == "Fort Aguada"
    assert scripts.shorten("Fort Aguada", 8) == "Fort Ag…"
    assert scripts.shorten("केदारनाथ", 4) == "के…"  # not "केद…" with the vowel sign of दा left behind
    assert scripts.shorten("क्षत्रिय", 4) == "क्ष…"


# ── Right to left ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("words", "drawn"),
    [
        (["Fort", "گلی", "12", "لال", "چوک", "walk"], ["Fort", "چوک", "لال", "12", "گلی", "walk"]),
        (
            ["سری", "نگر", "·", "10", "May"],
            ["نگر", "سری", "·", "10", "May"],
        ),  # numbers after the words keep their place
        (["(سری", "نگر)"], ["نگر)", "(سری"]),
        (["Goa", "beach", "2"], ["Goa", "beach", "2"]),
    ],
)
def test_right_to_left_words_are_drawn_in_reverse(words, drawn):
    assert scripts.visual_order(words, scripts.direction) == drawn


# Words of a sentence that are Urdu, among others. Each word is there once: a drawn word is told
# apart by its text.
SENTENCE = "Visit سرینگر جامع مسجد اور لال چوک today then ڈل جھیل کنارے شام walk back".split()
FIRST_LINE = scripts.text_width(" ".join(SENTENCE[:4]), theme.REGULAR, 10) + 2  # four words a line, not five


def _places(fragment) -> list[int]:
    """Where in SENTENCE the words a fragment holds are. A shaped word is glyphs: each word is shaped to compare."""
    places = []
    for text in str(fragment.text).split():
        matches = [
            at
            for at, word in enumerate(SENTENCE)
            if text in (word, str(shapeStr(word, fragment.fontName, fragment.fontSize)))
        ]
        assert len(matches) == 1, text
        places += matches
    return places


def _laid_out(paragraph: Paragraph) -> list[list[int]]:
    """The words of each line as ReportLab broke them — in reading order."""
    return [[at for fragment in line.words for at in _places(fragment)] for line in paragraph.blPara.lines]


def _drawn(flowables: list[Paragraph], width: float) -> list[list[int]]:
    """The words each line draws, from left to right, by their place in SENTENCE."""
    lines = []
    draw_line = reportlab_paragraph._putFragLine

    def watch(cur_x, tx, line, last, kind):
        lines.append([at for fragment in line.words for at in _places(fragment)])
        return draw_line(cur_x, tx, line, last, kind)

    canvas = Canvas(io.BytesIO())
    with patch.object(reportlab_paragraph, "_putFragLine", watch):
        for flowable in flowables:
            flowable.wrapOn(canvas, width, 1000)
            flowable.drawOn(canvas, 0, 0)
    return lines


def _sentence(words: list[str]) -> Paragraph:
    xml, shaped = scripts.markup(" ".join(words), theme.REGULAR)
    return scripts.paragraph(xml, BODY, shaped)


def test_a_paragraph_breaks_its_lines_in_reading_order_and_draws_each_right_to_left():
    """As a browser does: the first Urdu words on the first line, each line's Urdu reversed."""
    paragraph = _sentence(SENTENCE[:8])
    assert _drawn([paragraph], FIRST_LINE) == [[0, 3, 2, 1], [6, 5, 4, 7]]
    assert _laid_out(paragraph) == [[0, 1, 2, 3], [4, 5, 6, 7]]  # the layout itself is never reordered


def test_a_paragraph_split_across_two_pages_draws_each_part_right_to_left():
    whole = _sentence(SENTENCE)
    drawn_whole = _drawn([whole], FIRST_LINE)
    lines = _laid_out(whole)
    assert len(lines) > 2
    assert drawn_whole == [scripts.visual_order(line, lambda at: scripts.direction(SENTENCE[at])) for line in lines]

    paragraph = _sentence(SENTENCE)
    paragraph.wrapOn(Canvas(io.BytesIO()), FIRST_LINE, 1000)
    parts = paragraph.split(FIRST_LINE, BODY.leading * 2.5)  # room for two lines
    assert len(parts) == 2 and all(type(part) is scripts.ScriptParagraph for part in parts)
    assert _drawn(parts, FIRST_LINE) == drawn_whole


def test_one_line_on_the_canvas_is_drawn_right_to_left_too():
    """The running head and the chips are drawn straight onto the page, not as paragraphs."""
    canvas = Canvas(io.BytesIO())
    drawn = []
    with patch.object(canvas, "drawString", lambda x, y, text: drawn.append((x, str(text)))):
        end = scripts.draw_text(canvas, 10, 10, "سری نگر · 10 – 12 May", theme.REGULAR, 7.5)

    def word(text: str) -> str:
        for logical in ("سری", "نگر"):
            if text == str(shapeStr(logical, "NotoSansArabic-Regular", 7.5)):
                return logical
        return text

    assert [word(text) for _, text in drawn if text.strip()] == ["نگر", "سری", "·", "10", "–", "12", "May"]
    assert [x for x, _ in drawn] == sorted(x for x, _ in drawn)
    assert end == pytest.approx(10 + scripts.text_width("سری نگر · 10 – 12 May", theme.REGULAR, 7.5))


def test_a_chip_is_as_wide_as_its_label_in_any_script():
    chips = Chips(("मंदिर", "trekking"))
    chips.wrap(400, 100)
    widths = [width for _, _, width, _ in chips.placed]
    assert widths == [
        pytest.approx(scripts.text_width(label, Chips.FONT, Chips.SIZE) + 2 * Chips.PAD_X)
        for label in ("मंदिर", "trekking")
    ]


# ── The document ───────────────────────────────────────────────────────────


def _stop(name: str, lat: float, lng: float, cost: float = 0.0) -> dict:
    return {"activity": name, "cost": cost, "lat": lat, "lng": lng, "category": "religious", "rating": 6.0}


HOTEL = {
    "name": "होटल हिमालय दर्शन",
    "cost_per_night": 3800.0,
    "stars": 3,
    "rating": 8.1,
    "address": "गौरीकुंड मार्ग",
    "lat": 15.55,
    "lng": 73.76,
}
SCRIPT_TRIP = {
    "days": [
        {
            "day": 1,
            "date": str(START),
            "morning": _stop("श्री केदारनाथ मंदिर", 15.5, 73.8, 500.0),
            "afternoon": _stop("மீனாட்சி அம்மன் கோயில்", 15.51, 73.81),
            "evening": _stop("جامع مسجد دہلی", 15.52, 73.82),
            "hotel": HOTEL,
            "flight": flight(9100.0, day=str(START)),
        },
        {
            "day": 2,
            "date": str(START + timedelta(days=1)),
            "morning": _stop("ཐིག་སེ་དགོན་པ།", 15.53, 73.83),
            "afternoon": _stop("ಮೈಸೂರು ಅರಮನೆ", 15.54, 73.84, 120.0),
            "evening": _stop("দক্ষিণেশ্বর কালী মন্দির", 15.55, 73.85),
            "hotel": HOTEL,
            "flight": None,
        },
    ],
    "total_cost": 9100 + 2 * 3800 + 620,
}


def _fonts_by_page(pdf: bytes) -> list[set[str]]:
    return [
        {str(ref.get_object()["/BaseFont"]).split("+")[-1] for ref in page["/Resources"]["/Font"].values()}
        for page in PdfReader(io.BytesIO(pdf)).pages
    ]


def test_every_part_of_the_pdf_draws_the_scripts_of_india(map_picture):
    plan = _plan(
        SCRIPT_TRIP,
        destination="गोवा",
        end_date=START + timedelta(days=1),
        interests=["मंदिर", "ಹಂಪಿ", "beach"],
    )
    pdf = build_pdf(plan, map_picture)
    cover, *days, costs, on_the_map = _fonts_by_page(pdf)
    pages = _pages(pdf)

    # the cover: its title in Devanagari to match Fraunces's weight, chips in two scripts, the stay at a glance
    assert {"NotoSansDevanagari-Medium", "NotoSansKannada-Medium", "NotoSansDevanagari-Regular"} <= cover
    # every page after it: the running head names the destination as typed
    assert all(page.startswith("गोवा · 10 – 11 Dec 2027") for page in pages[1:])
    # the day cards: each stop's name and the hotel's address
    script_fonts = {f"{family}-Medium" for family in ("NotoSansTamil", "NotoSansArabic", "NotoSerifTibetan")}
    assert script_fonts | {"NotoSansKannada-Medium", "NotoSansBengali-Medium"} <= set().union(*days)
    # the ledger's stay line, and the map's key
    assert "NotoSansDevanagari-Regular" in costs
    assert script_fonts | {"NotoSansDevanagari-Medium"} <= on_the_map


# ── Download ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_file_is_named_in_the_destinations_own_script(tiles):
    exported = await export_itinerary(_plan(destination="गोवा"))
    assert (exported.filename, exported.ascii_filename) == ("trip-गोवा-2027-12-10.pdf", "trip-2027-12-10.pdf")

    trip = _trip(uuid.uuid4(), destination="गोवा")
    with _client(trip, _itinerary(trip)) as (client, _):
        response = await client.get(f"/trips/{trip.id}/export/pdf")
    assert response.status_code == 200
    # RFC 6266: `filename*` names it in Devanagari; plain `filename` is there for any client that cannot read that
    assert response.headers["content-disposition"] == (
        "attachment; filename=\"trip-2027-12-10.pdf\"; filename*=UTF-8''trip-"
        "%E0%A4%97%E0%A5%8B%E0%A4%B5%E0%A4%BE-2027-12-10.pdf"
    )
