"""Rebuild the TTFs in this directory (and in noto/) from the upstream variable fonts.

Not used at runtime and not a project dependency — run it only to change what
the PDF export embeds (another weight, more characters, another script):

    pip install fonttools certifi
    python src/backend/app/pdf/fonts/build_fonts.py

ReportLab cannot use a variable font: it draws the default instance, which for
Fraunces is "9pt Black". So each face is pinned to one static instance and cut
down to the characters it is there for:

- Inter and Fraunces, the site's typefaces: Latin with the diacritics of
  transliterated place names, Greek, Cyrillic, punctuation, ₹ and arrows. They
  are drawn as they are, unshaped, so their layout tables are left out.
- Noto for the scripts of India's languages (Phase 20) — Noto Sans, and for
  Tibetan Noto Serif, the only Noto there is for it: each script's own block
  plus ASCII and common punctuation, so that a word such as "(केदारनाथ)" is
  drawn in one font. These keep every GSUB / GPOS feature — complex scripts are
  shaped by HarfBuzz, and a conjunct is a substitution in those tables.

A rebuild gives the same bytes.
"""

import hashlib
import ssl
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import certifi
from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

HERE = Path(__file__).parent
NOTO = HERE / "noto"

# google/fonts at a fixed commit, so a rebuild starts from the same bytes.
UPSTREAM = "https://raw.githubusercontent.com/google/fonts/9710da1eacb3be272583c3224dcb70f9da6eadbb/ofl"

# family → (directory in google/fonts, file, SHA-256 of that file)
SOURCES = {
    "Inter": ("inter", "Inter[opsz,wght].ttf", "29160a80ff49ddcab2c97711247e08b1fab27a484a329ce8b813d820dc559031"),
    "Fraunces": (
        "fraunces",
        "Fraunces[SOFT,WONK,opsz,wght].ttf",
        "177ff6c0f14e5550a3c624247cd1189611d4eb65d000b14944c63d967958abbb",
    ),
    "NotoSansDevanagari": (
        "notosansdevanagari",
        "NotoSansDevanagari[wdth,wght].ttf",
        "14ec4af41f27482216d1c2229f417ff9b1425e1babb014e57d1d40d03229853e",
    ),
    "NotoSansBengali": (
        "notosansbengali",
        "NotoSansBengali[wdth,wght].ttf",
        "dcd42978094e584a849c84a51450eeac40c8826057d566ea6d4b9627a403a05a",
    ),
    "NotoSansGurmukhi": (
        "notosansgurmukhi",
        "NotoSansGurmukhi[wdth,wght].ttf",
        "1e6f728fa620e566f842d81e220265813faa12771214765d289c98e035adc5f2",
    ),
    "NotoSansGujarati": (
        "notosansgujarati",
        "NotoSansGujarati[wdth,wght].ttf",
        "9901d8552f1dd5d2c50dbd4caa6f6e174e74e8264f06594ab259ae6e7b1ac428",
    ),
    "NotoSansOriya": (
        "notosansoriya",
        "NotoSansOriya[wdth,wght].ttf",
        "910342275d619081b896b4ed25fd9ad6ff320c25591b810de1fcc4beb153ba82",
    ),
    "NotoSansTamil": (
        "notosanstamil",
        "NotoSansTamil[wdth,wght].ttf",
        "aa3a9b321f4b0bb2c40203ffbde9af89713227866e0e13f76e5b9eeea727cf88",
    ),
    "NotoSansTelugu": (
        "notosanstelugu",
        "NotoSansTelugu[wdth,wght].ttf",
        "e618af7bf999df192ed4f388eba2e563f2b5015034e9cbb317b5bd793bd7334d",
    ),
    "NotoSansKannada": (
        "notosanskannada",
        "NotoSansKannada[wdth,wght].ttf",
        "cca4f3b3a8cb12fb261f1b43baf5d2f7f59d90fe123d41f0065ed3a183997ec9",
    ),
    "NotoSansMalayalam": (
        "notosansmalayalam",
        "NotoSansMalayalam[wdth,wght].ttf",
        "312e0e7c3cc15fa09eb42a8f749eeb246b593ed420e3c81aafe8d910c3a6fb56",
    ),
    "NotoSansArabic": (
        "notosansarabic",
        "NotoSansArabic[wdth,wght].ttf",
        "63111b5b2e074dd48cc67692e0a2726d86ee94c1c37fe8598257b7b4e87e869e",
    ),
    "NotoSansOlChiki": (
        "notosansolchiki",
        "NotoSansOlChiki[wght].ttf",
        "c9c31988656f49eccec9588825ab3b5045099c2f850ef98f356f976e8a596b4d",
    ),
    "NotoSansMeeteiMayek": (
        "notosansmeeteimayek",
        "NotoSansMeeteiMayek[wght].ttf",
        "d56eb6d54ad8aad3570b7ee07f64866832a04f29bce6e5f183918c9eaf008fac",
    ),
    "NotoSerifTibetan": (
        "notoseriftibetan",
        "NotoSerifTibetan[wght].ttf",
        "060ec022b04c306de3f58d051fb0e1cf81a5b610c5910fbfc43bba154c057cda",
    ),
}

# Basic Latin, Latin-1, Latin Extended-A/B, Greek, Cyrillic, Latin Extended Additional, General
# Punctuation, currency signs (₹), arrows, minus, ★ and ✓.
LATIN = "U+0020-007E,U+00A0-024F,U+0370-03FF,U+0400-052F,U+1E00-1EFF,U+2000-206F,U+20A0-20BF,U+2190-2193,U+2212,U+2605,U+2713"

# file name → (family, style, where on the variation axes).
# Inter at opsz 14 is what the site serves. Fraunces: 36 is a display cut that still holds at
# 13 pt, and WONK 1 keeps the site's leaning h / m / n (the font's own default above 18 px).
FACES = {
    "Inter-Regular": ("Inter", "Regular", {"wght": 400, "opsz": 14}),
    "Inter-Medium": ("Inter", "Medium", {"wght": 500, "opsz": 14}),
    "Inter-SemiBold": ("Inter", "SemiBold", {"wght": 600, "opsz": 14}),
    "Fraunces-Medium": ("Fraunces", "Medium", {"wght": 500, "opsz": 36, "SOFT": 0, "WONK": 1}),
}

# What every script font keeps besides its own block: ASCII (punctuation and digits inside a word
# keep the word in one font), no-break space, middle dot, dashes, quotes, ellipsis, zero-width
# space / non-joiner / joiner, the dotted circle HarfBuzz shows a stray mark on, ₹, and the dandas.
SCRIPT_COMMON = "U+0020-007E,U+00A0,U+00B7,U+2010-2015,U+2018-201D,U+2026,U+200B-200D,U+25CC,U+20B9,U+0964-0965"

# The scripts India's languages and place names are written in, and the Unicode blocks of each.
# app/pdf/scripts.py picks a font by the same blocks.
SCRIPTS = {
    "NotoSansDevanagari": "U+0900-097F,U+A8E0-A8FF,U+1CD0-1CFF",  # Hindi, Marathi, Nepali, Sanskrit, Konkani…
    "NotoSansBengali": "U+0980-09FF",  # Bengali, Assamese
    "NotoSansGurmukhi": "U+0A00-0A7F",  # Punjabi
    "NotoSansGujarati": "U+0A80-0AFF",
    "NotoSansOriya": "U+0B00-0B7F",  # Odia
    "NotoSansTamil": "U+0B80-0BFF",
    "NotoSansTelugu": "U+0C00-0C7F",
    "NotoSansKannada": "U+0C80-0CFF",
    "NotoSansMalayalam": "U+0D00-0D7F",
    "NotoSansArabic": "U+0600-06FF,U+0750-077F,U+08A0-08FF,U+FB50-FDFF,U+FE70-FEFF",  # Urdu, Kashmiri, Sindhi
    "NotoSansOlChiki": "U+1C50-1C7F",  # Santali
    "NotoSansMeeteiMayek": "U+ABC0-ABFF,U+AAE0-AAFF",  # Manipuri
    "NotoSerifTibetan": "U+0F00-0FFF",  # Ladakhi, Sikkimese — monasteries in Ladakh are often named in it
}
SCRIPT_STYLES = {"Regular": 400, "Medium": 500}


def _download(family: str) -> bytes:
    directory, file, sha256 = SOURCES[family]
    url = f"{UPSTREAM}/{directory}/{urllib.parse.quote(file)}"
    # certifi's roots: the python.org build for macOS ships without any, and urllib then refuses every https URL
    context = ssl.create_default_context(cafile=certifi.where())
    with urllib.request.urlopen(url, timeout=60, context=context) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != sha256:
        sys.exit(f"{family}: the download does not match the recorded SHA-256 — upstream changed?")
    return data


def _download_licence(family: str) -> bytes:
    directory = SOURCES[family][0]
    context = ssl.create_default_context(cafile=certifi.where())
    with urllib.request.urlopen(f"{UPSTREAM}/{directory}/OFL.txt", timeout=60, context=context) as response:
        return response.read()


def _instance(source: Path, location: dict, family: str, style: str, file_name: str) -> TTFont:
    """One static face of a variable font, named as such."""
    # recalcTimestamp=False keeps upstream's modification time, so a rebuild gives the same bytes
    font = instancer.instantiateVariableFont(TTFont(source, recalcTimestamp=False), location)
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
    return font


def _subset(font: TTFont, unicodes: str, *, shaped: bool) -> None:
    options = subset.Options()
    # A shaped font keeps every layout feature: for a complex script they ARE the script.
    # An unshaped one keeps none — ReportLab would not apply them.
    options.layout_features = ["*"] if shaped else []
    options.hinting = False
    options.name_IDs = ["*"]  # keep the copyright and licence records
    options.notdef_outline = True  # a character the font lacks prints as a box, not as nothing
    options.glyph_names = False
    options.drop_tables += ["DSIG", "STAT", "gasp", "meta"]
    subsetter = subset.Subsetter(options)
    subsetter.populate(unicodes=subset.parse_unicodes(unicodes))
    subsetter.subset(font)


def build() -> None:
    work = HERE / ".upstream"
    work.mkdir(exist_ok=True)
    NOTO.mkdir(exist_ok=True)
    for family in SOURCES:
        (work / f"{family}.ttf").write_bytes(_download(family))

    for file_name, (family, style, location) in FACES.items():
        font = _instance(work / f"{family}.ttf", location, family, style, file_name)
        _subset(font, LATIN, shaped=False)
        font.save(HERE / f"{file_name}.ttf")
        print(f"{file_name}.ttf: {(HERE / f'{file_name}.ttf').stat().st_size / 1024:.0f} KB")

    for family, ranges in SCRIPTS.items():
        axes = {axis.axisTag for axis in TTFont(work / f"{family}.ttf")["fvar"].axes}
        for style, weight in SCRIPT_STYLES.items():
            file_name = f"{family}-{style}"
            location = {"wght": weight, **({"wdth": 100} if "wdth" in axes else {})}
            font = _instance(work / f"{family}.ttf", location, family, style, file_name)
            _subset(font, f"{SCRIPT_COMMON},{ranges}", shaped=True)
            font.save(NOTO / f"{file_name}.ttf")
            print(f"noto/{file_name}.ttf: {(NOTO / f'{file_name}.ttf').stat().st_size / 1024:.0f} KB")
        (NOTO / f"OFL-{family}.txt").write_bytes(_download_licence(family))

    for leftover in work.iterdir():
        leftover.unlink()
    work.rmdir()


if __name__ == "__main__":
    build()
