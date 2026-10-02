"""Rebuild the four TTFs in this directory from the upstream variable fonts.

Not used at runtime and not a project dependency — run it only to change what
the PDF export embeds (another weight, more characters):

    pip install fonttools certifi
    python src/backend/app/pdf/fonts/build_fonts.py

ReportLab cannot use a variable font: it draws the default instance, which for
Fraunces is "9pt Black". So each face is pinned to one static instance, then
cut down to the characters a trip plan uses — Latin with the diacritics of
transliterated place names, punctuation, the rupee sign and arrows. That is
about 57 KB a file instead of 880 KB.
"""

import hashlib
import ssl
import sys
import urllib.request
from pathlib import Path

import certifi
from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

HERE = Path(__file__).parent

# google/fonts at a fixed commit, so a rebuild starts from the same bytes.
UPSTREAM = "https://raw.githubusercontent.com/google/fonts/9710da1eacb3be272583c3224dcb70f9da6eadbb/ofl"
SOURCES = {
    "Inter": (
        f"{UPSTREAM}/inter/Inter%5Bopsz%2Cwght%5D.ttf",
        "29160a80ff49ddcab2c97711247e08b1fab27a484a329ce8b813d820dc559031",
    ),
    "Fraunces": (
        f"{UPSTREAM}/fraunces/Fraunces%5BSOFT%2CWONK%2Copsz%2Cwght%5D.ttf",
        "177ff6c0f14e5550a3c624247cd1189611d4eb65d000b14944c63d967958abbb",
    ),
}

# file name → (family, style, where on the variation axes).
# Inter at opsz 14 is what the site serves. Fraunces: 36 is a display cut that still holds at
# 13 pt, and WONK 1 keeps the site's leaning h / m / n (the font's own default above 18 px).
FACES = {
    "Inter-Regular": ("Inter", "Regular", {"wght": 400, "opsz": 14}),
    "Inter-Medium": ("Inter", "Medium", {"wght": 500, "opsz": 14}),
    "Inter-SemiBold": ("Inter", "SemiBold", {"wght": 600, "opsz": 14}),
    "Fraunces-Medium": ("Fraunces", "Medium", {"wght": 500, "opsz": 36, "SOFT": 0, "WONK": 1}),
}

# Basic Latin, Latin-1, Latin Extended-A/B, Latin Extended Additional, General Punctuation,
# currency signs (₹), arrows, minus, ★ and ✓.
UNICODES = "U+0020-007E,U+00A0-024F,U+1E00-1EFF,U+2000-206F,U+20A0-20BF,U+2190-2193,U+2212,U+2605,U+2713"


def _download(family: str) -> bytes:
    url, sha256 = SOURCES[family]
    # certifi's roots: the python.org build for macOS ships without any, and urllib then refuses every https URL
    context = ssl.create_default_context(cafile=certifi.where())
    with urllib.request.urlopen(url, timeout=60, context=context) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != sha256:
        sys.exit(f"{family}: the download does not match the recorded SHA-256 — upstream changed?")
    return data


def build() -> None:
    work = HERE / ".upstream"
    work.mkdir(exist_ok=True)
    for family in SOURCES:
        (work / f"{family}.ttf").write_bytes(_download(family))

    for file_name, (family, style, location) in FACES.items():
        # recalcTimestamp=False keeps upstream's modification time, so a rebuild gives the same bytes
        source = TTFont(work / f"{family}.ttf", recalcTimestamp=False)
        font = instancer.instantiateVariableFont(source, location)

        names = font["name"]
        for name_id, value in (
            (1, f"{family} {style}"),
            (2, "Regular"),
            (4, f"{family} {style}"),
            (6, file_name),
            (16, family),
            (17, style),
        ):
            names.setName(value, name_id, 3, 1, 0x409)
        names.names = [record for record in names.names if record.platformID == 3]

        options = subset.Options()
        options.layout_features = []  # neither ReportLab nor Pillow's basic layout applies GSUB / GPOS
        options.hinting = False
        options.name_IDs = ["*"]  # keep the copyright and licence records
        options.notdef_outline = True  # a character the font lacks prints as a box, not as nothing
        options.glyph_names = False
        options.drop_tables += ["DSIG", "STAT", "gasp"]
        subsetter = subset.Subsetter(options)
        subsetter.populate(unicodes=subset.parse_unicodes(UNICODES))
        subsetter.subset(font)

        path = HERE / f"{file_name}.ttf"
        font.save(path)
        print(f"{path.name}: {path.stat().st_size / 1024:.0f} KB, {len(font.getBestCmap())} characters")

    for leftover in work.iterdir():
        leftover.unlink()
    work.rmdir()


if __name__ == "__main__":
    build()
