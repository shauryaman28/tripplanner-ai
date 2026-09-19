# ItineraryBuilder System Prompt — v4

> Phase 16 — traveller preferences as context

## What changed from v3

The **system prompt is unchanged**. The *user* prompt gains one optional
paragraph, emitted only when the user has saved preferences:

```
Traveller preferences (context only — use them to choose among the PROVIDED
hotels, flights and attractions; never introduce a name that is not in the
data above): travel style: luxury; dietary restrictions: vegetarian; …
```

Preferences reach the builder via `trip_meta["preferences"]`, so no signature
changed (`build_itinerary`, `ItineraryBuilder.run` are untouched). With no saved
preferences the prompt is byte-identical to v3.

## Why the guard sentence is inside the block

The builder's data-scope rule (v1) is the most important constraint in the
prompt. A preference such as "vegetarian" invites the model to write a
restaurant name; the block therefore restates that preferences only *select
among* provided data. `_validate_data_scope` still rejects any invented name
regardless (defence-in-depth, see v3 note).

## Test cases to run against the live model (NOT yet run)

| # | Input | Expected |
|---|---|---|
| 1 | full data + `dietary_restrictions=["vegetarian"]` | every activity name ∈ provided attractions |
| 2 | empty attractions + vegetarian | `"Explore the area"`, **no** invented restaurant |
| 3 | no preferences | output identical in shape to v3 |

## Still failing / out of scope

Preferences can only influence *choice among* provided data; they cannot add
data. Making dietary preferences change *what is searched* is handled upstream
(interests injection) and is limited by the attractions tool's interest mapping.
