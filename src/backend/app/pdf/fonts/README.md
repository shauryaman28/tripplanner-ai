# Fonts embedded in the exported PDF

| File | Face | Used for |
|---|---|---|
| `Inter-Regular.ttf`, `Inter-Medium.ttf`, `Inter-SemiBold.ttf` | Inter, optical size 14 | text, labels, figures, the numbers in the pins |
| `Fraunces-Medium.ttf` | Fraunces, optical size 36, weight 500 | headings |

They are the site's two typefaces (`src/frontend/src/app/layout.tsx`), so the PDF looks like the
page it was downloaded from — and Inter has the rupee sign, which ReportLab's built-in fonts do not.

## Licence

Both are under the SIL Open Font License 1.1: `OFL-Inter.txt` and `OFL-Fraunces.txt`. The licence
has to travel with the fonts, so keep those two files in this directory. Neither font declares a
Reserved Font Name, so these modified versions may keep the names "Inter" and "Fraunces".

## How they were made

`build_fonts.py` downloads the upstream variable fonts (google/fonts, at a fixed commit, checked
against a recorded SHA-256), pins each face to one static instance and keeps only the characters a
trip plan uses: Latin with the diacritics of transliterated place names, punctuation, currency
signs (₹) and arrows. A rebuild gives the same bytes.

Two reasons they are not the upstream files as they come:

- **ReportLab cannot use a variable font.** It draws the default instance, which for Fraunces is
  "9pt Black" — every heading would come out in the heaviest weight there is.
- **Size.** The four files are 232 KB together (55–59 KB each). The two upstream variable fonts
  are 877 KB and 360 KB.

## What they do not cover

Scripts other than Latin — Devanagari, for one. Such text prints as empty boxes; the PDF is still
built, and its file name falls back to the trip's date. Headings that need a character Fraunces
lacks (it has no arrows) are set in Inter instead.
