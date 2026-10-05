"""The PDF itself: cover, day by day, local tips, cost breakdown, map — laid out with ReportLab.

ReportLab rather than WeasyPrint (DECISIONS #100): WeasyPrint draws through
Pango and GObject, which have to be installed on the machine — it would not
even import on the laptop this was built on. ReportLab is one pure-Python
wheel, so the same PDF is built in the unit tests, in CI and in Docker.

build_pdf() is synchronous and all CPU: call it from a worker thread.
"""

from __future__ import annotations

import io
import math
import threading
from datetime import date

from reportlab.lib.colors import HexColor, white
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    BaseDocTemplate,
    Flowable,
    Frame,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from app.pdf import scripts, theme
from app.pdf.flowables import (
    DAY_BAR,
    HAIRLINE,
    INK_100,
    INK_200,
    INK_400,
    INK_500,
    INK_900,
    PIN,
    STATUS,
    Bookmark,
    Brand,
    BudgetPill,
    Card,
    Chips,
    Legend,
    MapPicture,
    Meter,
    PinMark,
    draw_logo,
)
from app.pdf.formatting import format_date, format_date_range, format_inr, format_weekday, plural
from app.pdf.plan import (
    SLOT_LABELS,
    TIP_SECTIONS,
    Day,
    Flight,
    LocalTips,
    MapFeatures,
    Stay,
    Stop,
    TripPlan,
    budget_note,
    flight_facts,
    flight_route,
    map_features,
    nights_stayed,
    stay_facts,
    stay_totals,
    stop_facts,
)

# A registered TrueType face is one object shared by every document, and saving a document reads
# its font subsets through that object's state. Two documents saved at once corrupt each other —
# IndexError / KeyError inside ReportLab's makeSubset, 40 builds out of 96 in a trial without this
# lock. A build takes a few tens of milliseconds, so they simply take turns.
_BUILDING = threading.Lock()

PAGE_WIDTH, PAGE_HEIGHT = A4
MARGIN = 18 * mm
CONTENT_WIDTH = PAGE_WIDTH - 2 * MARGIN
BODY_TOP, BODY_BOTTOM = 24 * mm, 20 * mm
COVER_TOP, COVER_BOTTOM = 20 * mm, 30 * mm

FINE_PRINT = (
    "Prices are the ones found when this trip was planned and can change. Nothing in this plan has been booked."
)
# Local tips come from a model's knowledge, not from a search (Phase 22): said wherever they are shown.
TIPS_NOTE = (
    "General advice from the assistant's own knowledge of the place, not from a live source. "
    "Prices and timings change — check locally."
)


# ── Text ───────────────────────────────────────────────────────────────────


def _style(name: str, font: str, size: float, leading: float, colour: str = theme.INK_800, **extra) -> ParagraphStyle:
    return ParagraphStyle(name, fontName=font, fontSize=size, leading=leading, textColor=HexColor(colour), **extra)


EYEBROW = _style("eyebrow", theme.SEMIBOLD, 6.8, 9.5, theme.INK_500, textTransform="uppercase")
COVER_EYEBROW = _style("cover-eyebrow", theme.SEMIBOLD, 8, 11, theme.INK_500, textTransform="uppercase")
COVER_FACTS = _style("cover-facts", theme.REGULAR, 12, 17, theme.INK_600)
H2 = _style("h2", theme.DISPLAY, 19, 24, theme.INK_900)
LEAD = _style("lead", theme.REGULAR, 9, 13, theme.INK_600)
DAY_TITLE = _style("day-title", theme.DISPLAY, 13.5, 17, theme.INK_900)
ROW_TITLE = _style("row-title", theme.MEDIUM, 10, 13.5, theme.INK_900)
ROW_TITLE_QUIET = _style("row-title-quiet", theme.MEDIUM, 10, 13.5, theme.INK_600)
META = _style("meta", theme.REGULAR, 8, 11.5, theme.INK_600)
AMOUNT = _style("amount", theme.SEMIBOLD, 9.5, 12.5, theme.INK_900, alignment=TA_RIGHT)
AMOUNT_NOTE = _style("amount-note", theme.REGULAR, 7, 9.5, theme.INK_500, alignment=TA_RIGHT)
BODY = _style("body", theme.REGULAR, 9.5, 13.5)
BODY_STRONG = _style("body-strong", theme.SEMIBOLD, 9.5, 13.5, theme.INK_900)
FIGURE = _style("figure", theme.REGULAR, 9.5, 13.5, alignment=TA_RIGHT)
FIGURE_STRONG = _style("figure-strong", theme.SEMIBOLD, 9.5, 13.5, theme.INK_900, alignment=TA_RIGHT)
TABLE_HEAD = _style("table-head", theme.SEMIBOLD, 6.8, 9.5, theme.INK_500, textTransform="uppercase")
TABLE_HEAD_RIGHT = ParagraphStyle("table-head-right", parent=TABLE_HEAD, alignment=TA_RIGHT)
TILE_LABEL = _style("tile-label", theme.MEDIUM, 7.5, 10, theme.INK_500)
TILE_VALUE = _style("tile-value", theme.SEMIBOLD, 14, 18, theme.INK_900)
TILE_NOTE = _style("tile-note", theme.REGULAR, 7.5, 10, theme.INK_500)
TOTAL = _style("total", theme.SEMIBOLD, 34, 38, theme.INK_900)
SMALL = _style("small", theme.REGULAR, 7.5, 10.5, theme.INK_500)
KEY = _style("key", theme.REGULAR, 8.5, 11.5, theme.INK_600)


def _p(text: str, style: ParagraphStyle) -> Paragraph:
    """Text from a plan is data, never markup. A word in another script is set in a font for it (scripts.py)."""
    xml, shaped = scripts.markup(text, style.fontName)
    return scripts.paragraph(xml, style, shaped)


def _amount(value: float, note: str = "") -> list[Paragraph]:
    return [_p(format_inr(value), AMOUNT), *([_p(note, AMOUNT_NOTE)] if note else [])]


# A card or a table row cannot run on to the next page, so one absurdly long name in an itinerary
# would make the whole document impossible to lay out. Names and detail lines are cut to these.
NAME_LIMIT, DETAIL_LIMIT, MAX_CHIPS = 140, 240, 12
TIP_LIMIT = 320  # one local tip


# ── Cover ──────────────────────────────────────────────────────────────────


def _cover_title_style(destination: str) -> ParagraphStyle:
    """One line for a place name; smaller type when someone's destination is a sentence."""
    size = 42 if len(destination) <= 16 else 32 if len(destination) <= 32 else 23
    return _style("cover-title", theme.DISPLAY, size, size * 1.08, theme.INK_900)


def _cost_card(plan: TripPlan) -> Card:
    """The trip's one headline number, and how it sits against the budget."""
    note = budget_note(plan.costs.total, plan.budget)
    headline = [_p("Estimated total", EYEBROW), _p(format_inr(plan.costs.total), TOTAL)]
    left = CONTENT_WIDTH * 0.52
    rows: list[list] = [[headline, BudgetPill(note) if note else ""]]
    commands = [
        ("VALIGN", (0, 0), (0, 0), "TOP"),
        ("VALIGN", (1, 0), (1, 0), "BOTTOM"),
        ("LEFTPADDING", (0, 0), (-1, -1), 16),
        ("RIGHTPADDING", (0, 0), (-1, -1), 16),
        ("TOPPADDING", (0, 0), (-1, 0), 14),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, -1), (-1, -1), 15),
    ]
    if note:
        rows += [
            [Meter(note), ""],
            [_p(f"{note.percent}% of your {format_inr(plan.budget)} budget", SMALL), ""],
        ]
        commands += [
            ("SPAN", (0, 1), (1, 1)),
            ("SPAN", (0, 2), (1, 2)),
            ("TOPPADDING", (0, 1), (-1, 1), 12),
            ("TOPPADDING", (0, 2), (-1, 2), 6),
        ]
    return Card(Table(rows, colWidths=[left, CONTENT_WIDTH - left], style=TableStyle(commands)), CONTENT_WIDTH)


def _at_a_glance(plan: TripPlan) -> Table:
    """The three things a trip is made of, one line each."""
    flight = plan.flight
    if flight:
        travel = " · ".join(part for part in (flight_route(flight), *flight_facts(flight)) if part)
    else:
        travel = "Return flights" if plan.costs.flights > 0 else "Not included in this plan"

    stays = stay_totals(plan)
    if stays:
        stay = "; ".join(f"{scripts.shorten(entry.name, 80)} · {plural(entry.nights, 'night')}" for entry in stays)
    else:
        stay = "Not included in this plan"

    places = sum(1 for day in plan.days for stop in day.stops if not stop.free_time)
    outline = f"{plural(len(plan.days), 'day')} · {plural(places, 'place')} to see"

    rows = [
        [_p(label, EYEBROW), _p(scripts.shorten(words, DETAIL_LIMIT), BODY)]
        for label, words in (("Flight", travel), ("Stay", stay), ("Plan", outline))
    ]
    return Table(
        rows,
        colWidths=[52, CONTENT_WIDTH - 52],
        style=TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (0, -1), 10.5),  # the small caps sit on the first line of the words beside them
                ("LINEBELOW", (0, 0), (-1, -2), HAIRLINE, INK_200),
            ]
        ),
    )


def _cover(plan: TripPlan) -> list[Flowable]:
    facts = " · ".join(
        (
            format_date_range(plan.start_date, plan.end_date),
            plural(plan.nights, "night"),
            plural(plan.travellers, "traveller"),
        )
    )
    story: list[Flowable] = [
        Brand(),
        Spacer(1, 34 * mm),
        _p("Trip itinerary", COVER_EYEBROW),
        Spacer(1, 4),
        _p(scripts.shorten(plan.destination, 200), _cover_title_style(plan.destination)),
        Spacer(1, 8),
        _p(facts, COVER_FACTS),
    ]
    if plan.interests:
        story += [Spacer(1, 10), Chips(tuple(scripts.shorten(interest, 40) for interest in plan.interests[:MAX_CHIPS]))]
    return [*story, Spacer(1, 16 * mm), _cost_card(plan), Spacer(1, 9 * mm), _at_a_glance(plan)]


# ── Day by day ─────────────────────────────────────────────────────────────

_MARK_COLUMN, _AMOUNT_COLUMN = 12 + PIN, 92


def _flight_row(flight: Flight) -> list:
    route, facts = flight_route(flight), flight_facts(flight)
    body = [
        _p("Flight", EYEBROW),
        _p(scripts.shorten(route or (facts[0] if facts else "Flight"), NAME_LIMIT), ROW_TITLE),
    ]
    if facts:
        body.append(_p(scripts.shorten(" · ".join(facts), DETAIL_LIMIT), META))
    price = _amount(flight.price, "return") if flight.price is not None else ""
    return [PinMark(theme.AIRPORT_STYLE, icon="plane"), body, price]


def _stop_row(day: int, stop: Stop) -> list:
    if stop.free_time:  # it only ever stands alone (plan.py): the whole day, not a slot
        words = [
            _p("All day", EYEBROW),
            _p("Free time", ROW_TITLE_QUIET),
            _p("Nothing booked — explore the area at your own pace.", META),
        ]
        return ["", words, ""]

    mark = PinMark(theme.day_style(day), label=str(stop.order)) if stop.order else PinMark(theme.UNMAPPED_STYLE)
    body = [_p(SLOT_LABELS[stop.slot], EYEBROW), _p(scripts.shorten(stop.name, NAME_LIMIT), ROW_TITLE)]
    if facts := stop_facts(stop):
        body.append(_p(scripts.shorten(" · ".join(facts), DETAIL_LIMIT), META))
    return [mark, body, _amount(stop.cost, "entry fees") if stop.cost > 0 else ""]


def _stay_row(stay: Stay) -> list:
    body = [_p("Stay", EYEBROW), _p(scripts.shorten(stay.name, NAME_LIMIT), ROW_TITLE)]
    if facts := stay_facts(stay):
        body.append(_p(scripts.shorten(" · ".join(facts), DETAIL_LIMIT), META))
    return [PinMark(theme.HOTEL_STYLE, icon="bed"), body, _amount(stay.cost_per_night, "per night")]


def _day_card(day: Day) -> Card:
    """One day: its colour down the side, then the flight, each stop and the stay — the page's day card."""
    title = [_p(f"Day {day.number}", EYEBROW), _p(format_weekday(day.date) or f"Day {day.number}", DAY_TITLE)]
    rows = [[title, "", _amount(day.cost, "stay + activities") if day.cost > 0 else ""]]
    if day.flight:
        rows.append(_flight_row(day.flight))
    rows += [_stop_row(day.number, stop) for stop in day.stops]
    if day.stay:
        rows.append(_stay_row(day.stay))
    if len(rows) == 1:
        rows.append(["", [_p("Nothing is planned for this day.", META)], ""])

    body_column = CONTENT_WIDTH - DAY_BAR - _MARK_COLUMN - _AMOUNT_COLUMN
    table = Table(
        rows,
        colWidths=[_MARK_COLUMN, body_column, _AMOUNT_COLUMN],
        style=TableStyle(
            [
                ("SPAN", (0, 0), (1, 0)),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (0, -1), 12),
                ("RIGHTPADDING", (0, 0), (0, -1), 0),
                ("LEFTPADDING", (1, 0), (1, -1), 9),
                ("RIGHTPADDING", (1, 0), (1, -1), 6),
                ("LEFTPADDING", (2, 0), (2, -1), 0),
                ("RIGHTPADDING", (2, 0), (2, -1), 14),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("LINEABOVE", (0, 1), (-1, -1), HAIRLINE, INK_100),
            ]
        ),
    )
    return Card(table, CONTENT_WIDTH, bar=theme.day_style(day.number).color)


def _days(plan: TripPlan, with_map: bool) -> list[Flowable]:
    lead = (
        "Each day has its own colour — the same one on the map."
        if with_map
        else "Morning, afternoon and evening, with the flight and where you stay."
    )
    story: list[Flowable] = [Bookmark("Day by day"), _p("Day by day", H2), _p(lead, LEAD), Spacer(1, 12)]
    for day in plan.days:
        story += [_day_card(day), Spacer(1, 9)]
    return story


# ── Local tips ─────────────────────────────────────────────────────────────


def _named(name: str, words: str, style: ParagraphStyle) -> Paragraph:
    """A name in strong type and what is said about it. Both are text from a plan, so neither is markup."""
    name_xml, name_shaped = scripts.markup(scripts.shorten(name, 80), theme.MEDIUM)
    words_xml, words_shaped = scripts.markup(scripts.shorten(words, TIP_LIMIT), style.fontName)
    strong = f'<font name="{theme.MEDIUM}" color="{theme.INK_900}">{name_xml}</font>'
    return scripts.paragraph(f"{strong} — {words_xml}", style, name_shaped or words_shaped)


def _tips_card(label: str, items: list[Paragraph]) -> Card:
    """One kind of tip — its heading, then each tip on a line of its own."""
    rows = [[_p(label, EYEBROW)], *([item] for item in items)]
    table = Table(
        rows,
        colWidths=[CONTENT_WIDTH],
        style=TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 14),
                ("RIGHTPADDING", (0, 0), (-1, -1), 14),
                # close-set: five sections of three or four tips each are one page, not one and a bit
                ("TOPPADDING", (0, 0), (-1, -1), 4.5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4.5),
                ("TOPPADDING", (0, 0), (-1, 0), 10),
                ("BOTTOMPADDING", (0, -1), (-1, -1), 10),
                ("LINEABOVE", (0, 2), (-1, -1), HAIRLINE, INK_100),
            ]
        ),
    )
    return Card(table, CONTENT_WIDTH)


def _tips(tips: LocalTips) -> list[Flowable]:
    """The page's "Local tips" section: getting around, customs, traps, best times, safety — those there are."""

    def lines(items: tuple[str, ...]) -> list[Paragraph]:
        return [_p(scripts.shorten(item, TIP_LIMIT), BODY) for item in items]

    by_section = {
        "local_transport": lines((tips.transport,) if tips.transport else ()),
        "cultural_norms": lines(tips.customs),
        "tourist_traps": lines(tips.traps),
        "best_times": [_named(place, when, BODY) for place, when in tips.best_times],
        "safety_tips": lines(tips.safety),
    }
    story: list[Flowable] = [Bookmark("Local tips"), _p("Local tips", H2), _p(TIPS_NOTE, LEAD), Spacer(1, 12)]
    for key, label in TIP_SECTIONS:
        if by_section[key]:
            story += [_tips_card(label, by_section[key]), Spacer(1, 8)]
    return story


# ── Cost breakdown ─────────────────────────────────────────────────────────


def _cost_tiles(plan: TripPlan) -> Card:
    """Flights / stay / activities — the three tiles under the page's total."""
    costs, nights = plan.costs, nights_stayed(plan)
    travellers = plural(plan.travellers, "traveller")
    tiles = (
        ("Flights", costs.flights, f"Return, {travellers}" if costs.flights > 0 else "Not included"),
        ("Stay", costs.stay, plural(nights, "night") if costs.stay > 0 else "Not included"),
        ("Activities", costs.activities, "Entry fees" if costs.activities > 0 else "No entry fees listed"),
    )
    cells = [
        [_p(label, TILE_LABEL), _p(format_inr(value), TILE_VALUE), _p(note, TILE_NOTE)] for label, value, note in tiles
    ]
    table = Table(
        [cells],
        colWidths=[CONTENT_WIDTH / 3] * 3,
        style=TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 14),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 11),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
                ("LINEAFTER", (0, 0), (-2, -1), HAIRLINE, INK_200),
            ]
        ),
    )
    return Card(table, CONTENT_WIDTH)


def _ledger(plan: TripPlan) -> Table:
    """What the total is made of, line by line. It adds up to the total it prints."""
    costs = plan.costs
    flight = plan.flight
    if costs.flights <= 0:
        flight_words = "Not included"
    else:
        parts = (flight_route(flight) if flight else "", "return", plural(plan.travellers, "traveller"))
        flight_words = " · ".join(part for part in parts if part)

    lines = [
        scripts.markup(
            f"{scripts.shorten(entry.name, 80)} · {plural(entry.nights, 'night')} × {format_inr(entry.cost_per_night)}",
            BODY.fontName,
        )
        for entry in stay_totals(plan)
    ]
    stay_words = (
        scripts.paragraph("<br/>".join(xml for xml, _ in lines), BODY, any(shaped for _, shaped in lines))
        if lines
        else _p("Not included", BODY)
    )

    rows: list[list] = [
        [_p("Flights", BODY_STRONG), _p(flight_words, BODY), _p(format_inr(costs.flights), FIGURE)],
        [_p("Stay", BODY_STRONG), stay_words, _p(format_inr(costs.stay), FIGURE)],
        [
            _p("Activities", BODY_STRONG),
            _p("Entry fees" if costs.activities > 0 else "No entry fees listed", BODY),
            _p(format_inr(costs.activities), FIGURE),
        ],
    ]
    if abs(costs.unexplained) >= 1:
        other = _p(format_inr(costs.unexplained), FIGURE)
        rows.append([_p("Other", BODY_STRONG), _p("Part of the plan's total", BODY), other])
    total_row = len(rows)
    rows.append([_p("Estimated total", BODY_STRONG), "", _p(format_inr(costs.total), FIGURE_STRONG)])

    commands = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LINEBELOW", (0, 0), (-1, total_row - 2), HAIRLINE, INK_200),
        ("LINEABOVE", (0, total_row), (-1, total_row), 1, INK_900),
    ]

    note = budget_note(costs.total, plan.budget)
    if note:
        _, ink, _ = STATUS[note.state]
        words = {"under": "Under budget", "tight": "Left of the budget", "over": "Over budget"}[note.state]
        gap_style = ParagraphStyle("gap", parent=FIGURE_STRONG, textColor=HexColor(ink))
        rows += [
            [_p("Your budget", BODY), "", _p(format_inr(plan.budget), FIGURE)],
            [_p(words, BODY), "", _p(format_inr(abs(plan.budget - costs.total)), gap_style)],
        ]
        commands.append(("LINEBELOW", (0, total_row), (-1, total_row), HAIRLINE, INK_200))
    if plan.travellers > 1 and costs.total > 0:
        share = _p(format_inr(costs.total / plan.travellers), FIGURE)
        rows.append([_p("Per traveller", BODY), _p(f"{format_inr(costs.total)} ÷ {plan.travellers}", BODY), share])

    return Table(rows, colWidths=[104, CONTENT_WIDTH - 104 - 96, 96], style=TableStyle(commands))


def _daily_costs(plan: TripPlan) -> Table:
    """Each day's share. Flights are bought once, so they have a line of their own."""
    costs = plan.costs
    head = [_p("Day", TABLE_HEAD), _p("Date", TABLE_HEAD)]
    head += [_p(words, TABLE_HEAD_RIGHT) for words in ("Stay", "Activities", "Day total")]
    rows: list[list] = [head]
    for day in plan.days:
        figures = [_p(format_inr(value), FIGURE) for value in (day.stay_cost, day.activities_cost, day.cost)]
        rows.append([_p(str(day.number), BODY), _p(format_weekday(day.date), BODY), *figures])
    last_day = len(rows) - 1

    if costs.flights > 0:
        rows.append([_p("Flights", BODY), "", "", "", _p(format_inr(costs.flights), FIGURE)])
    if abs(costs.unexplained) >= 1:
        rows.append([_p("Other", BODY), "", "", "", _p(format_inr(costs.unexplained), FIGURE)])
    totals = [_p(format_inr(value), FIGURE_STRONG) for value in (costs.stay, costs.activities, costs.total)]
    rows.append([_p("Total", BODY_STRONG), "", *totals])

    return Table(
        rows,
        colWidths=[36, CONTENT_WIDTH - 36 - 3 * 86, 86, 86, 86],
        repeatRows=1,
        style=TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 5.5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5.5),
                ("LINEBELOW", (0, 0), (-1, 0), HAIRLINE, INK_400),
                ("LINEBELOW", (0, 1), (-1, last_day), HAIRLINE, INK_100),
                ("LINEABOVE", (0, -1), (-1, -1), 1, INK_900),
            ]
        ),
    )


def _costs(plan: TripPlan) -> list[Flowable]:
    return [
        Bookmark("Cost breakdown"),
        _p("Cost breakdown", H2),
        _p("What the estimated total is made of.", LEAD),
        Spacer(1, 12),
        _cost_tiles(plan),
        Spacer(1, 14),
        _ledger(plan),
        Spacer(1, 20),
        _p("Day by day", EYEBROW),
        Spacer(1, 2),
        _daily_costs(plan),
        Spacer(1, 16),
        _p(FINE_PRINT, SMALL),
    ]


# ── Map ────────────────────────────────────────────────────────────────────


def _key_words(name: str, what: str) -> Paragraph:
    """A place's name, in strong type, and what it is to the trip: "Fort Aguada — Day 1, morning"."""
    xml, shaped = scripts.markup(scripts.shorten(name, 80), theme.MEDIUM)
    return scripts.paragraph(f'<font name="{theme.MEDIUM}" color="{theme.INK_900}">{xml}</font> — {what}', KEY, shaped)


def _map_key(features: MapFeatures) -> Table | None:
    """Which place each pin is: the numbers on the picture, spelled out."""
    entries: list[tuple[PinMark, Paragraph]] = []
    for pin in features.activities:
        words = _key_words(pin.name, f"Day {pin.day}, {SLOT_LABELS[pin.slot].lower()}")
        entries.append((PinMark(theme.day_style(pin.day), label=str(pin.order), size=12), words))
    for hotel in features.hotels:
        entries.append((PinMark(theme.HOTEL_STYLE, icon="bed", size=12), _key_words(hotel.name, "where you stay")))
    for airport in features.airports:
        if airport.role == "destination":  # the departure airport is off the picture
            name = f"{airport.code} · {airport.name}" if airport.name else airport.code
            mark = PinMark(theme.AIRPORT_STYLE, icon="plane", size=12)
            entries.append((mark, _key_words(name, "where you land")))
    if not entries:
        return None

    half = math.ceil(len(entries) / 2)  # two columns, read down the first and then the second
    rows = []
    for index in range(half):
        right = entries[index + half] if index + half < len(entries) else ("", "")
        rows.append([*entries[index], *right])
    text_column = (CONTENT_WIDTH - 2 * 18) / 2
    return Table(
        rows,
        colWidths=[18, text_column, 18, text_column],
        style=TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (1, 0), (1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        ),
    )


def _map(features: MapFeatures, image: bytes) -> list[Flowable]:
    legend = [(theme.day_style(day), "", f"Day {day}") for day in features.days]
    if features.hotels:
        legend.append((theme.HOTEL_STYLE, "bed", "Hotel"))
    if any(airport.role == "destination" for airport in features.airports):
        legend.append((theme.AIRPORT_STYLE, "plane", "Airport"))

    story: list[Flowable] = [
        Bookmark("On the map"),
        _p("On the map", H2),
        _p(f"{plural(len(features.activities), 'stop')}, numbered in visiting order within each day.", LEAD),
        Spacer(1, 12),
        MapPicture(image, CONTENT_WIDTH),
        Spacer(1, 10),
        Legend(legend),
        Spacer(1, 8),
    ]
    if key := _map_key(features):
        story.append(key)
    if features.unmapped:
        story += [Spacer(1, 8), _p(f"Not shown on the map (no location data): {'; '.join(features.unmapped)}", SMALL)]
    return story


# ── Pages ──────────────────────────────────────────────────────────────────


def _draw_cover_page(canv: Canvas, prepared_on: date) -> None:
    """A faint sunrise behind the top of the page, as on the site, and the small print at the foot."""
    # Two glows that fade to white before they meet: where two overlap, the second would paint
    # over the first, and blend modes that avoid that are not drawn alike by every PDF viewer.
    for x, y, radius, tint in (
        (PAGE_WIDTH * 0.10, PAGE_HEIGHT + 40, PAGE_WIDTH * 0.60, "#fdf1d6"),
        (PAGE_WIDTH * 0.97, PAGE_HEIGHT + 30, PAGE_WIDTH * 0.27, "#fcecf2"),
    ):
        canv.radialGradient(x, y, radius, [HexColor(tint), white], extend=False)

    canv.saveState()
    canv.setFillColor(INK_500)
    canv.setFont(theme.REGULAR, 7.5)
    canv.drawString(MARGIN, 17 * mm, f"Prepared on {format_date(prepared_on)}.")
    canv.drawString(MARGIN, 17 * mm - 10.5, FINE_PRINT)
    canv.restoreState()


def _draw_body_page(canv: Canvas, page: int, pages: int | None, running_title: str, prepared_on: date) -> None:
    """The running head and foot of every page after the cover."""
    canv.saveState()
    top = PAGE_HEIGHT - 14 * mm
    canv.setFillColor(INK_500)
    scripts.draw_text(canv, MARGIN, top, running_title, theme.REGULAR, 7.5)
    wordmark = pdfmetrics.stringWidth("TripPlanner", theme.DISPLAY, 9.5)
    canv.setFillColor(INK_900)
    canv.setFont(theme.DISPLAY, 9.5)
    canv.drawString(PAGE_WIDTH - MARGIN - wordmark, top - 0.5, "TripPlanner")
    draw_logo(canv, PAGE_WIDTH - MARGIN - wordmark - 16, top - 3.2, 11.5)
    canv.setStrokeColor(INK_200)
    canv.setLineWidth(HAIRLINE)
    canv.line(MARGIN, top - 7, PAGE_WIDTH - MARGIN, top - 7)

    canv.setFillColor(INK_500)
    canv.setFont(theme.REGULAR, 7.5)
    canv.drawString(MARGIN, 11 * mm, f"Prepared on {format_date(prepared_on)}")
    canv.drawRightString(PAGE_WIDTH - MARGIN, 11 * mm, f"Page {page} of {pages}" if pages else f"Page {page}")
    canv.restoreState()


def _render(plan: TripPlan, map_image: bytes | None, prepared_on: date, pages: int | None) -> tuple[bytes, int]:
    """Lay the document out once. Returns the PDF and how many pages it came to."""
    date_range = format_date_range(plan.start_date, plan.end_date)
    running_title = f"{scripts.shorten(plan.destination, 60)} · {date_range}"
    if not scripts.drawable(running_title, theme.REGULAR):
        running_title = date_range  # in a script there is no font for (Chinese…) it would print as boxes

    out = io.BytesIO()
    document = BaseDocTemplate(
        out,
        pagesize=A4,
        title=f"{plan.destination} — {date_range}",
        author="TripPlanner AI",
        subject="Trip itinerary",
        creator="TripPlanner AI",
        lang="en",
        displayDocTitle=True,
        initialFontName=theme.REGULAR,  # otherwise every page declares Helvetica, which nothing uses
    )

    def frame(name: str, top: float, bottom: float) -> Frame:
        height = PAGE_HEIGHT - top - bottom
        return Frame(
            MARGIN, bottom, CONTENT_WIDTH, height, leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0, id=name
        )

    document.addPageTemplates(
        [
            PageTemplate(
                id="cover",
                frames=[frame("cover", COVER_TOP, COVER_BOTTOM)],
                onPage=lambda canv, _doc: _draw_cover_page(canv, prepared_on),
            ),
            PageTemplate(
                id="body",
                frames=[frame("body", BODY_TOP, BODY_BOTTOM)],
                onPage=lambda canv, doc: _draw_body_page(canv, doc.page, pages, running_title, prepared_on),
            ),
        ]
    )

    story: list[Flowable] = [*_cover(plan), NextPageTemplate("body"), PageBreak()]
    story += _days(plan, with_map=map_image is not None)
    if plan.tips:  # Phase 22 — after the days, as on the page
        story += [PageBreak(), *_tips(plan.tips)]
    story += [PageBreak(), *_costs(plan)]
    if map_image is not None:
        story += [PageBreak(), *_map(map_features(plan), map_image)]

    document.build(story)
    return out.getvalue(), document.page


def build_pdf(plan: TripPlan, map_image: bytes | None = None, prepared_on: date | None = None) -> bytes:
    """The itinerary as a PDF: cover, day by day, local tips when the plan has them, cost breakdown and — when
    there is a picture — the map.

    Laid out twice: the foot of each page says "Page 2 of 5", and the 5 is only
    known once the first pass has run. A flowable cannot be drawn twice, so
    each pass builds its own.
    """
    prepared_on = prepared_on or date.today()
    with _BUILDING:
        _, pages = _render(plan, map_image, prepared_on, pages=None)
        pdf, _ = _render(plan, map_image, prepared_on, pages=pages)
    return pdf
