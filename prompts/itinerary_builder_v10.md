# ItineraryBuilder Prompt — v10

> 2026-10-17 — Phase 36: a plan in Hindi, Tamil or Bengali
> Model: Groq `GROQ_MODEL` (default `openai/gpt-oss-120b`), `temperature=0`

## What changed from v9

The system prompt is unchanged, and so is the prompt of every plan in English — byte for byte
(`tests/fixtures/phase36/english_plan.json` records it, and a test compares every commit with it). On a
plan in another language (`trip_meta["language"]`: `hi`, `ta` or `bn`) the **user prompt** has one more
paragraph, just before its last line:

```
The traveller reads Hindi. Give every slot except "Explore the area" two more keys, both written in Hindi,
in the Hindi script: "local_name" — the place's name as Hindi speakers write it ("Baga Beach" → "बागा बीच");
"description" — one sentence of at most 25 words on what to do there, from the attraction's category and
description. "activity" stays the place's name exactly as the attractions list gives it, in English: it is
how the plan is checked, mapped and booked. JSON keys, dates and numbers stay as they are.
```

The example name is the language's own: "பாகா கடற்கரை" in Tamil, "বাগা সৈকত" in Bengali.

## Why

The roadmap asks for "activity descriptions in local script where available". A stop had no description
at all — only its name, copied verbatim from the search, because that name is how the plan is checked
(data scope), pinned (its coordinates are looked up by it), costed, and matched to the local tips. So the
name stays and the language goes into two new keys beside it.

## What the code does around the prompt

1. **Kept only when they are what was asked for** (`_keep_local_text`): a description or local name in
   the language's script, cut to 320 and 80 characters. One in English or another script is let go; on a
   plan in English both are dropped, whatever the model wrote.
2. **What the builder does not write is translated** after the evaluator has passed the plan
   (`src/ai/language.localise`, prompts/translator_v1.md): a free day, a stop the group repair moved in, a
   description that was let go — from the attraction's own description — and the local tips.
3. **A change keeps the words of the stops it keeps** (`earlier_local_text`). Asked to change only the
   hotel, the model copies the plan it is shown — names only — and writes no local name or description
   again (live run F below). The words come from the plan before.
4. **The plan says its language** (`language`), set by code; one in the model's reply is dropped.

## Live runs (2026-10-17, production-like stack)

| Run | Builder tokens | Stops described in the language | Local names |
|---|---|---|---|
| Hindi, Goa, 5 days | 5,715 | 10 of 10 | 10 |
| Tamil, Madurai, 3 days | 4,545 | 9 of 9 | 9 |
| Bengali, Kolkata, 3 days | 4,614 | 9 of 9 | 9 |
| Hindi chosen, English request, Jaipur | 4,460 | 9 of 9 | 9 |
| English, Udaipur, 3 days (v9's prompt) | 3,915 | — | — |
| The Hindi plan, its hotel changed | 5,222 | 0 of 10 written by the model | 0 — now kept by code |

Samples: "Rachol Church" → "राचोल चर्च", "आध्यात्मिक शांति के लिए राचोल चर्च में सुंदर वास्तुकला देखें।";
"Sri Meenakshi Amman Temple" → "ஸ்ரீ மீனாட்சி அம்மன் கோவில்"; "marble Palace" → "মার্বেল প্যালেস".
No `activity` came back in another script; every plan passed the evaluator first time but for Groq's
per-minute limit (DECISIONS #339).

## Still failing / out of scope

- The quality of the sentences is the model's, read by eye; nothing grades it.
- A local name is the model's spelling of a place, not looked up anywhere: "Excavated site in Chandor"
  came back as "चैंडोर में उत्खनित स्थल" in one run and "खोदाई स्थल चंदोर में" in the next.
