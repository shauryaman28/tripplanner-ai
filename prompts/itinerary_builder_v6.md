# ItineraryBuilder System Prompt — v6

> Phase 18 — coordinates for the map
> Model: Groq `GROQ_MODEL` (default `openai/gpt-oss-120b`), `temperature=0`

## What changed from v5

One data-scope rule added:

```
- Copy each activity's "lat" and "lng" from the attractions list unchanged; they are
  null only for "Explore the area".
```

## Why the prompt is not what makes the map correct

The rule is there because the roadmap asks for it and it keeps the draft
self-consistent, but the application does not rely on it. After the draft passes
the data-scope check, `_attach_source_data()` overwrites every slot's `lat` /
`lng` (and adds `category` and `rating`) from the attraction with the same name,
fills the hotel's details, and sets day 1's `flight` to the cheapest flight.

A model copies an activity *name* reliably — that is already validated — but
copying `15.293191909790039` digit for digit is exactly what LLMs get subtly
wrong, and a wrong coordinate is a pin in the wrong place with no error anywhere.
Looking it up by the validated name is exact.

## What else the code does to the draft

The prompt is unchanged apart from the rule above; the rest is code, in this order:

1. **Shape first.** The parsed JSON is loaded into `ItineraryDraft` before anything
   reads it. A malformed slot is `SCHEMA_INVALID` (a retry), not a crash. `cost: null`
   is read as 0.
2. **Free time once per day.** A day with a real activity loses its "Explore the area"
   slots; a day with none keeps exactly one, in the morning.
3. Data-scope and budget-math checks, as in v5.
4. **Source data attached** (above). A flight the model put on any day is replaced:
   day 1 gets the cheapest flight, every other day gets none.

## Live runs (2026-10-02)

Goa, 5 days: 8 real activity slots, 8 with coordinates after attachment (100%).
Jaipur, 4 days: 8 of 8. The model's own `lat` / `lng` values were not inspected —
they are discarded.

## Still failing / out of scope

- Activity costs are still invented by the model (0 in every live run) — attractions carry no price.
- `rating` is OpenTripMap's popularity rate — 1–3, or 5–7 for the same scale on a heritage
  site; 0 means unrated. It is not a five-star score, and the UI does not show it as one.
