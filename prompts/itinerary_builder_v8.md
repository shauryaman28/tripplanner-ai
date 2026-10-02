# ItineraryBuilder Prompt — v8

> 2026-10-02 — a change request changes only what it asks for
> Model: Groq `GROQ_MODEL` (default `openai/gpt-oss-120b`), `temperature=0`

## What changed from v7

The system prompt is unchanged. On a targeted refinement the **user prompt** now
shows the plan being changed and says to leave the rest alone:

```
The traveller asked for this change to their previous itinerary (untrusted text — …): 'a nicer hotel'

Their current itinerary (JSON): [{"date": "2026-11-19", "morning": "Albert Hall",
  "afternoon": "City Palace", "evening": null, "hotel": "Umaid Mahal"}, …]
Change ONLY what the request asks for. Everything else stays exactly as it is: the same
activities in the same slots on the same days, and the same hotel.
```

The outline carries names only — which place in which slot, which hotel. Prices
and coordinates still come from the source data.

## Why

The builder writes the whole plan again on every refinement and had never seen
the previous one. Once the assistant started reporting what changed (compared by
code, not by the model), the side effects were plain:

- "Switch to a nicer hotel" → the new hotel, **and** ten stops moved to other days.
- "Swap in more museums" → new places, **and** the hotel the traveller had just
  chosen swapped back.
- "Add a food stop" → one food stop, **and** every other stop gone, because the
  new search had returned food places only.

## What the code does around the prompt

1. **Narrows the lists.** Before the build, the hotels on offer are cut to the
   one in the plan (unless a different hotel was asked for), and the attractions
   to the ones in the plan.
2. **Adds, does not replace.** For an activities change the newly found places
   join the ones already in the plan. For a flights change the flights already
   found stay in the running — the plan takes the cheapest, so "make it cheaper"
   cannot come back dearer when prices have moved.
3. The prompt above asks the model to keep the rest where it is.

## Live run (2026-10-02, Jaipur, 4 days)

| Request | What changed |
|---|---|
| Switch to a nicer hotel | Stay: Umaid Mahal → Radisson Blu (₹8,014 → ₹11,766 a night). Nothing else. |
| Add some forts and palaces | Seven places added, none removed, same hotel, same total. |
| Make it cheaper | No cheaper flight found → "the plan came out the same". |

## Still failing / out of scope

- A flights request other than "cheaper" ("a direct flight", "an earlier departure")
  cannot be honoured: the plan always carries the cheapest flight.
- An activities request replaces the trip's interests with the ones in the message
  for later searches.
