# Phase 36 — Multi-Language Support Foundation

**Status: built, and checked on the production-like stack on real models (Gemini `gemini-3.5-flash-lite`, Groq `openai/gpt-oss-120b` and `gpt-oss-20b`, sandbox flight and hotel providers): a request in each of Hindi, Tamil and Bengali planned in that language, a plan in Hindi chosen for an English request, an English plan untouched, and a change typed in Hindi. NOT on a deployment: there is none yet.** What that leaves open is in [Known limits](#known-limits).
**What the phase delivers (roadmap):** A Hindi query produces a correctly structured itinerary. Language preference stored in user preferences.

Decisions: `DECISIONS.md` #331–#339 (#331 is what was tested, and what failed). How to run it: `HOW_TO_RUN.md`, Step 34.

Four things are worth knowing before reading on.

- **The models did not need the translation; the tools did.** Gemini and Groq's small model parsed the roadmap's request — and requests in Tamil and Bengali — as typed exactly as well as translated. What reads only English is the code under them: the flight tool found no airport for "गोवा" or "மதுரை", and the attraction search turned "समुद्र तट" (beaches) and "खाना" (food) into the general sights. That is what the translation is for (#332).
- **English sees zero change, and that is recorded, not claimed.** Before any of the phase's code was written, two English trips were run through the whole planner and every prompt sent to every model was saved with the plan and the run's state. Every commit is held to that record: the same prompts, byte for byte (#336).
- **`langdetect` alone would have planned "Goa" in Somali, "गोवा" in Marathi — and Hindi keywords in English.** It is right about whole sentences and wrong about much of what travellers actually type: "हिमालय पर्वत यात्रा" is Marathi to it 40 times in 40 (found by the break-on-purpose run). The script of the text decides first, and Marathi or Nepali needs a word of its own (#331).
- **Found live: asked to change only the hotel, the builder dropped every stop's Hindi.** It copied the plan it was shown — the names — and wrote no local name or description again. Code now keeps the words of every stop a change keeps (#339).

## What was built

```
src/ai/language.py                         ← NEW: read (script, then langdetect), translate (one call, each string checked),
                                             localise (a finished plan's tips and missing descriptions in its language)
src/ai/orchestrator/orchestrator.py        ← the language node first in the graph; the localise node before persist; both in
                                             a refinement's run too; the language and a typed destination carried forward
src/ai/builder/builder.py                  ← on a plan in another language: local_name and description on every stop, kept
                                             only in the language's script; `language` on the plan; English untouched
src/ai/llm.py                              ← ask() takes a longer answer's tokens and timeout; two more callers counted
migrations/versions/008_preferred_language.py, app/models + schemas + routes/users.py
                                           ← user_preferences.preferred_language: en, hi, ta, bn or none
app/api/routes/trips.py                    ← the timeline says what the two new steps did
app/pdf/{plan,document}.py                 ← a stop's local name and sentence under its name; a free day in the language
src/frontend: lib/language.ts, components/PlanLanguageMenu.tsx (NEW); AppHeader, DayCard, LocalTips, ItineraryView,
              lib/{api,types}.ts           ← the language chosen in the header; the plan's words marked with their `lang`
prompts/itinerary_builder_v10.md, prompts/translator_v1.md (NEW)
tests/fixtures/phase36/                    ← the English record, and the script that made it
tests/unit/test_phase36_language.py, tests/integration/test_phase36_language_integration.py, e2e/language.spec.ts (NEW)
docs/phase36_live/                         ← the live runs, as they were recorded
requirements.txt, constraints.txt          ← langdetect 1.0.9
```

## Dev A — input language handling

- **`LanguageDetectionNode`** — `language_node`, the graph's entry. It reads everything the traveller wrote — the request, a change request, the destination, the trip's interests, each traveller's interests — by script, and by `langdetect` within the script (#331). Hindi, Tamil or Bengali is translated into English in one call (`translate`, prompts/translator_v1.md) before intent parsing; anything else is left as typed. The plan's language is the traveller's saved choice, else the request's, else the trip's so far, else English (#333).
- **The preference** — `user_preferences.preferred_language`, migration 008; `PUT /users/preferences` takes `en`, `hi`, `ta`, `bn` or null; the site's header saves it. Set, it decides the plan's language and detection does not; it is never learned from a request (#333).
- **The final itinerary, in the detected language** — `localise_node`, between the evaluator and saving: the local tips, a free day, and any stop the builder did not describe (#335).
- **Supported, tested, failed** — DECISIONS #331 (the reader) and #339 (the live runs).

## Dev B — itinerary localisation

- **The builder writes in the target language** — on a plan in Hindi, Tamil or Bengali every stop gets a `local_name` and a `description` in that language's script, while `activity` stays the English name every check and the map go by (#334, prompts/itinerary_builder_v10.md).
- **It persists** — the choice is a row; signed out and signed in again it is still there, and every trip after it is planned in it (integration test); a change to a plan keeps the plan's language (#333).
- **The site and the PDF** show the local name and the sentence under each stop's name, marked with their `lang`, a badge "Written in हिन्दी", the tips in the language; the page's own words stay English (#338).

## Found and fixed along the way

- **A destination typed in Devanagari became "Goa" on its trip** — Phase 20's test of a PDF named and headed in Devanagari failed: the run searched with "Goa" and then wrote it back to the trip. The trip now keeps the name its traveller typed, unless the plan moved somewhere else (#337).
- **A change typed in Hindi lost the plan's Hindi** — live, on the production-like stack; fixed by code, not by the prompt (#339).
- **The header's new menu pushed four pages 30 px past a phone's width** — the four "fits a phone" browser tests failed; on a phone the menu has no icon and a narrower box.
- **`ask()` could not translate a plan** — its Groq stand-in had 1,024 tokens and 15 s, made for one-line answers; a plan's tips in Tamil need more. A translation asks for 6,144 and 30 s; every other caller asks as before.

## The order things were done in

09:14 the branch from main (Phase 35 merged). 09:17 `langdetect` probed on 30 inputs, then 9 mixed ones, before any code: right about sentences, wrong about short text (#331). 09:23 the English record captured on the unchanged code — twice, identical (the two files' times). Then the reader, the translator, the nodes and the builder, and an English plan run again against the record: identical. 09:46 the first commit, pushed. 09:47 the translator and intent parsing on the real models (`docs/phase36_live/translations.txt`, `intent_as_typed.txt`). 09:52 the production-like stack rebuilt; 09:54–09:57 six live runs (`docs/phase36_live/plans.json`). The change-request finding was fixed after them, and committed at 14:46 (the session stood idle in between).

## Live results

### The translator and intent parsing (17 October, 09:47)

Nine requests — three in each language — through `translate` on Gemini: all nine correct, in 1.2–3.8 s a language. Bengali digits came back as digits (৫০,০০০ → 50,000). The roadmap's request, translated, parsed to destination Goa, budget ₹50,000 — and parsed the same as typed. Groq's small model, Gemini's stand-in, too. The request names no dates and no length the schema has a field for: "5 दिन" is read nowhere, as "5 days" was not before this phase — the trip's own dates decide.

### Six runs on the production-like stack (17 October, 09:54–09:57)

| Run | Took | Language | Stops described in it | Local names | Notes |
|---|---|---|---|---|---|
| A — "गोवा में 5 दिन की यात्रा, बजट ₹50,000", destination typed "गोवा" | 17.4 s | hi (the request) | 10 of 10 | 10 | translation 1.2 s, localise 3.4 s (15 strings); the trip still "गोवा" |
| B — a Tamil request to Madurai, destination typed "மதுரை" | 30.7 s | ta | 9 of 9 | 9 | localise 18 strings, none kept in English |
| C — a Bengali request about Kolkata, destination typed "কলকাতা" | 33.8 s | bn | 9 of 9 | 9 | the tips' model and the first build were turned away by Groq's limit (below) |
| D — an English request, Hindi chosen | 33.8 s | hi (the choice) | 9 of 9 | 9 | no translation asked for |
| E — an English request, nothing chosen | 36.9 s | — | — | — | no `language` or `localise` row; no new key in the plan |
| F — A changed: "होटल समुद्र तट के पास चाहिए" | 40.6 s | hi (the trip) | 10 of 10 | **0** | the finding below |

Every plan passed the evaluator; no stop's name was in another script. Samples (A): "Rachol Church" → "राचोल चर्च", "आध्यात्मिक शांति के लिए राचोल चर्च में सुंदर वास्तुकला देखें।"; (B) "Sri Meenakshi Amman Temple" → "ஸ்ரீ மீனாட்சி அம்மன் கோவில்"; (C) "marble Palace" → "মার্বেল প্যালেস".

**F, the finding.** The refinement classifier read the Hindi change correctly (targeted_hotel); the language node translated it ("I want a hotel near the beach"); and the builder, shown the plan's names and told to change only the hotel, wrote the plan back with no local name or description on any stop. The localise step described all ten from the attractions' own English ("A popular historical attraction in Goa." → "गोवा में एक लोकप्रिय ऐतिहासिक आकर्षण।"), so the plan was still all Hindi — but the builder's sentences and every local name were gone. Fixed in code: a stop a change keeps keeps what the plan said of it (#339); a unit test reproduces the builder's behaviour and holds the words.

**The rate limit.** Five of the six runs had a model call turned away by Groq's free tier — 8,000 tokens a minute on `gpt-oss-120b` — because six plans were asked for in three minutes. Every one was asked again and answered; the English run (E) was turned away too. It is not the language, but the language costs tokens: the builder's call for a plan in Hindi, Tamil or Bengali was 4,460–5,715 tokens against 3,915 for English (+14 % to +46 %).

### The check, again, on the fix (17 October, 17:26–17:31)

The stack rebuilt with the change fix, and `python scripts/language_check.py --prodlike --all`
(`docs/phase36_live/evening_check.txt`): A, B, C and F held every check — **F, the change typed in Hindi, kept
the words of all ten stops it kept**, and every stop had its Hindi name. D made no plan: its builder call waited
out Groq's per-minute limit past the builder's 30 s deadline (`LLM_TIMEOUT`, and Phase 31 does not build again
after a model that did not answer), the fifth plan in three minutes. The script crashed on a plan not made; it
now says so as a check, and runs can be chosen (`--runs DE`). D and E again on their own a minute later: D
planned in 14.5 s, its builder in 8.1 s, every stop in Hindi; 12 of 12. Without a rate limit in the way, the
builder took 6.9–9.3 s for a plan in another language and 7.3 s for the English one in this run; the
translation 0.8–1.9 s, the localisation 2.2–3.3 s.

### The PDF

A plan in Hindi printed (`docs/phase36_pdf_hindi.png`): each stop's English name, its Hindi name and its sentence under it, conjuncts joined and vowel signs placed (त्र, र्त, स्तं, द्र), the free day in Hindi.

## Tests

| | New | Total |
|---|---|---|
| Unit + contract | 94 (`test_phase36_language.py`) — and 6 earlier ones that now expect the two new steps or the new field | 1,622 |
| Integration | 5 (`test_phase36_language_integration.py`) — and 3 earlier ones updated | 101 |
| Browser | 4 (`e2e/language.spec.ts`) | 68 |

The three detection inputs per language of the roadmap are `REQUESTS` in the unit tests; the English record is `test_english_sees_zero_change`.

## Break it on purpose

On a scratch copy of the repository (its own database `tripplanner_m36`, Redis number 13, and, for the site,
its own stub backend and Next server on ports 8136 and 3136), each breakage alone, the tests run after it:
unit first, the integration suite for those that need the database, the browser test for the site's.

**First round: 43 of 46 caught.** Caught: every way of reading the language wrongly (Latin letters read, one
word left to langdetect, Assamese as Bengali, Marathi as Hindi), every way a translation could be let in
wrongly (still in its own script, in no letter of the target, longer than it should be, a list of another
length), the prompt not calling the text untrusted, the model given no room; the tips left in English, the
evaluator's draft changed under it, a surprise failing the plan, a free day undescribed; an English plan
logging a row, the choice ignored, the trip's language not carried, a group's interests not translated, the
state's list changed in place, the connection held through a translation, any saved language believed, the
typed destination renamed or kept when the plan moved; the plan never localised, a change request not read,
a full replan forgetting its language, a change not localised; every English plan asked for Hindi, an English
description kept on a Hindi plan, a model's description kept on an English one, null keys saved on every plan,
a model's language believed, a plan not saying its language; the choice not saved (integration), any language
taken by the API, the translator not counted, the timeline unlabelled, the PDF's local text not drawn or read;
and on the site, a stop's words not shown, the tips not marked, the menu not saving, no badge.

Survived, and what was done:

| Breakage | Why it survived | Now |
|---|---|---|
| translate: every string in one call (`MAX_PER_CALL` 40 → 4000) | the test sent `MAX_PER_CALL + 5` strings — it grew with the setting it tested | 45 strings, and the sizes of the calls asserted (40 and 5): caught |
| db: the column takes anything | the test tried a bad language before migrating down and up again — on the constraint an earlier run had left, not the one the migration makes | tried after the round trip: caught |
| read: `langdetect` unseeded | the tests' texts are ones it is sure of | **still survives** — and led to the next finding |
| (site: the menu does not save) | caught, but only when the whole test ran out (120 s) | the save is waited for 15 s on its own: caught as that |

**Found by looking for a text that unseeded `langdetect` reads differently:** "गोवा यात्रा बजेट होटल" — Hindi
keywords — came back Marathi 15 times in 40, and "हिमालय पर्वत यात्रा" Marathi 40 times in 40, seeded or
not. A Hindi traveller who typed keywords was planned in English. Marathi and Nepali now need a word of their
own in the text (#331).

**Second round: 7 of 8 caught** — the three above again, the menu again (failing in 15 s), and four breakages
of the new rule (Marathi believed with no word of its own, the other language's words ignored, Nepali's endings
unknown, Marathi's words unknown). The unseeded `langdetect` still survives: with the words deciding Marathi
and Nepali, no text was found whose answer its coin changes. In all, 49 of the 50 breakages caught.

## Done criterion checklist

- [x] "गोवा में 5 दिन की यात्रा, बजट ₹50,000" → correctly parsed (destination Goa, budget ₹50,000 — live), itinerary produced in Hindi (live run A: every stop described in Hindi)
- [x] English-input users see zero change — the recorded prompts and plan, on every commit; live run E: no new row, no new key
- [x] Language detection correct for 3 test inputs per language — nine of nine (unit test; and live, langdetect at ≥ 0.9999)
- [x] Supported languages documented with what was tested and what failed — DECISIONS #331, #339
- [x] Hindi-language itinerary stored in `structured_data` with Hindi in its descriptions — integration test; live run A
- [x] `preferred_language` persists across sessions — integration test (signed out, in again) and the browser test; live run D

## Known limits

- **Romanised Hindi is English to the planner** ("Goa mein 5 din"), and planned in English. A request in Marathi under 12 letters, or with no word only Marathi has ("मुंबई ते पुणे प्रवास"), is read as Hindi and planned in Hindi. (#331)
- **The site's own words are English.** Headings, buttons, the progress panel's messages, the tips' section names: a plan's language is not the site's language.
- **Marathi, Nepali, Assamese, Telugu, Urdu** are read and named, and planned in English.
- **A plan in another language costs more tokens** (up to ~46 % more for the builder) and one more short model call each way; on Groq's free tier, plans close together reach its per-minute limit sooner.
- **The quality of the Hindi, Tamil and Bengali is the models'.** It was read by eye for these runs; there is no grader for it (Phase 33's grader reads English).
