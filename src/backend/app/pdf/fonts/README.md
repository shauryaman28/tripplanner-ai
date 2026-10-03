# Fonts embedded in the exported PDF

| File | Face | Used for |
|---|---|---|
| `Inter-Regular.ttf`, `Inter-Medium.ttf`, `Inter-SemiBold.ttf` | Inter, optical size 14 | text, labels, figures, the numbers in the pins |
| `Fraunces-Medium.ttf` | Fraunces, optical size 36, weight 500 | headings |
| `noto/<Family>-Regular.ttf`, `noto/<Family>-Medium.ttf` | Noto, for the scripts of India (Phase 20) | a word Inter and Fraunces cannot draw |

Inter and Fraunces are the site's two typefaces (`src/frontend/src/app/layout.tsx`), so the PDF
looks like the page it was downloaded from — and Inter has the rupee sign, which ReportLab's
built-in fonts do not. They draw Latin (with the diacritics of transliterated place names), Greek
and Cyrillic.

The Noto families in `noto/` draw the rest of what a trip in India is written in:

| Family | Script | Languages |
|---|---|---|
| Noto Sans Devanagari | Devanagari | Hindi, Marathi, Nepali, Sanskrit, Konkani, Bodo, Dogri, Maithili |
| Noto Sans Bengali | Bengali | Bengali, Assamese |
| Noto Sans Gurmukhi | Gurmukhi | Punjabi |
| Noto Sans Gujarati | Gujarati | Gujarati |
| Noto Sans Oriya | Odia | Odia |
| Noto Sans Tamil | Tamil | Tamil |
| Noto Sans Telugu | Telugu | Telugu |
| Noto Sans Kannada | Kannada | Kannada |
| Noto Sans Malayalam | Malayalam | Malayalam |
| Noto Sans Arabic | Arabic | Urdu, Kashmiri, Sindhi |
| Noto Sans Ol Chiki | Ol Chiki | Santali |
| Noto Sans Meetei Mayek | Meetei Mayek | Manipuri |
| Noto Serif Tibetan | Tibetan | Ladakhi, Sikkimese — OpenTripMap names many monasteries in it |

Tibetan is a Noto *Serif*: there is no Noto Sans Tibetan. Body text uses the Regular weight;
everything set in Inter Medium or SemiBold, and the Fraunces headings, use Medium.

`app/pdf/scripts.py` picks the font for each word and has HarfBuzz (`uharfbuzz`, installed with
`reportlab[shaping]`) shape the words in these scripts: that is what joins conjuncts, places vowel
signs and joins Arabic letters. Without it the letters would print side by side.

## Licence

All of them are under the SIL Open Font License 1.1: `OFL-Inter.txt`, `OFL-Fraunces.txt`, and
`noto/OFL-<Family>.txt` for each Noto family. The licence has to travel with the fonts, so keep
those files beside them. None of the fonts declares a Reserved Font Name, so these modified
versions may keep their names.

## How they were made

`build_fonts.py` downloads the upstream variable fonts (google/fonts, at a fixed commit, checked
against a recorded SHA-256), pins each face to one static instance and keeps only the characters it
is there for. A rebuild gives the same bytes.

- **Inter and Fraunces** keep Latin, Greek, Cyrillic, punctuation, currency signs (₹) and arrows.
  They are drawn as they are, so their layout tables are left out.
- **Noto** keeps its script's own Unicode blocks, plus ASCII and common punctuation — so that
  "(केदारनाथ)" or "श्री-Krishna" is one word in one font — and every layout feature: for these
  scripts the substitutions and positioning in those tables *are* the script.

Two reasons they are not the upstream files as they come:

- **ReportLab cannot use a variable font.** It draws the default instance, which for Fraunces is
  "9pt Black" — every heading would come out in the heaviest weight there is.
- **Size.** Inter and Fraunces are 295 KB together (55–80 KB each), against 1.2 MB upstream. The
  26 Noto files are 3.6 MB; Tibetan alone is 1.3 MB of that (thousands of stacked letters). A PDF
  embeds only the glyphs it uses, so a plan without Tibetan carries none of it.

## What they do not cover

Scripts no font here has — Chinese, Thai, Hebrew… — print as empty boxes; the PDF is still built,
the running head leaves the destination out, and the file name keeps it only in `filename*` (a
destination with no ASCII letters is plain `trip-<date>.pdf` to an old client). The planner serves
trips within India, so these come up only when someone types them.

Copying text out of the PDF: Devanagari and most Latin come out as typed, but a shaped word in
Tamil, Urdu or another script whose letters are reordered or joined may come out in the order of
its glyphs. Printing and reading are unaffected.
