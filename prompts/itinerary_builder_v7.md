# ItineraryBuilder Prompt — v7

> 2026-10-02 — the plan must cover every day of the trip
> Model: Groq `GROQ_MODEL` (default `openai/gpt-oss-120b`), `temperature=0`

## What changed from v6

The system prompt is unchanged. The **user prompt** now states the number of days
instead of leaving the model to count them:

```
Trip: Goa from 2026-12-10 to 2026-12-14, 2 traveller(s).
That is 5 days: "days" must have exactly 5 entries, one for every date from
2026-12-10 to 2026-12-14 inclusive, in order — also when there are fewer
attractions than days.
```

## Why

A trip created as 28 Feb 2027 – 28 Feb 2028 (366 days) came back as **one** day,
and a second attempt as three: with eight attractions and no hotel the model
stopped when it ran out of things to place. The evaluator had no check for it,
so a one-day plan for a year was saved as "planned".

Three changes, of which the prompt is the smallest:

1. The prompt spells out the day count (above).
2. The evaluator has a `missing_days` check: every date from start to end must
   have exactly one day in the draft. It is the safety net — the model is asked,
   the code verifies.
3. A trip can be at most 14 nights (`MAX_TRIP_NIGHTS`): with at most ten
   attractions, a longer plan is mostly empty days.

## Live run (2026-10-02)

Udaipur, 4 days (3 nights): four entries, dates consecutive, hotel on the first
three days, none on the departure day.

## Still failing / out of scope

- Retries do not tell the model what the evaluator rejected, so a `missing_days`
  failure that survives the prompt will fail the trip after three identical attempts.
- Activity costs are still 0 — attractions carry no price.
