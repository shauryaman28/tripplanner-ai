# Phase 16 — User Preferences & Personalisation

**Status: 🟡 Code complete — verification pending (see checklist; boxes are only ticked once run)**

**Done criterion (roadmap):** `dietary_restrictions = ["vegetarian"]` → ActivitiesAgent MCP call includes
`"vegetarian"` in interests without being asked. `home_city = "Delhi"` → FlightAgent uses `DEL` as origin.
`PreferenceExtractorNode` updates `travel_style` after a luxury trip. `PUT /users/preferences` overwrites.
Preferences persist across sessions.

## What was built

```
migrations/versions/003_add_user_preferences.py        ← NEW  table + CHECK constraint
src/backend/app/
├── core/preferences.py                                ← NEW  pure normalise / merge helpers
├── models/user_preferences.py                         ← NEW  UserPreferences (PK = user_id)
├── schemas/user_preferences.py                        ← NEW  PreferencesUpdate / PreferencesRead
├── api/routes/users.py                                ← NEW  GET / PUT /users/preferences
├── models/__init__.py, schemas/__init__.py            ← +1 export each
├── core/config.py                                     ← +ANTHROPIC_API_KEY
└── main.py                                            ← +users router
src/ai/
├── utils/preferences.py                               ← NEW  load / inject / additive-merge
├── agents/preference_extractor.py                     ← NEW  Claude Haiku 4.5 + heuristic fallback
├── agents/flight_agent.py                             ← +preferred_airlines passthrough
├── mcp_server/{models,tools}.py                       ← +preferred_airlines (soft rank)
├── builder/builder.py                                 ← +preferences paragraph in user prompt
└── orchestrator/orchestrator.py                       ← +2 nodes, +3 one-line flight inputs
prompts/preference_extractor_v1.md, itinerary_builder_v4.md
tests/unit/test_phase16_preferences.py
```

## Graph (Phase 16)

```
intent_parsing → apply_preferences → run_flight → budget_decision → … → persist → merge → extract_preferences → END
```

## Data flow

| Preference | Injected as | Where it takes effect |
|---|---|---|
| `home_city` | `state["origin"]` (only if none given) | every FlightAgent call (initial, replan, retry, refine) |
| `dietary_restrictions` | appended to `state["interests"]` | ActivitiesAgent → `get_attractions` |
| `travel_style` | default interests, only when trip has none | ActivitiesAgent |
| `preferred_airlines` | `flight_input["preferred_airlines"]` | MCP `search_flights` soft-ranks carriers |
| all four | `trip_meta["preferences"]` | ItineraryBuilder user prompt |

## Behaviour worth knowing

- **PUT = replace; extractor = add-only.** Omitted PUT fields reset to empty.
- **No `ANTHROPIC_API_KEY`:** extractor skips the LLM and still sets `travel_style` from the heuristic.
- **Every new node writes an `agent_runs` row** (`preferences`, `preference_extractor`) — Phase 13's "no silent nodes" rule.
- **Nothing here can fail a trip:** both nodes catch everything and pass state through.

## Known limitations

1. Inferred `travel_style` never self-corrects (no provenance column) — DECISIONS #39.
2. Extractor sees only the `/plan` `raw_input`, not `/clarify` or `/refine` messages.
3. `vegetarian` ranks rather than filters — `get_attractions` maps unknown interests to `interesting_places`.
4. `resolve_origin_code` imports a private helper from the MCP tool module — DECISIONS #42.
5. Concurrent first-time `PUT`s for one user would collide on the PK (500). Unlikely; not handled.
6. Timeline labels: `preferences` / `preference_extractor` fall back to auto-title-case in `GET /trips/{id}/timeline`; add entries to `_AGENT_LABELS` if you want friendlier text.

## Verification checklist

Run in this order from the repo root after applying `MODIFIED_FILES.md`:

```bash
pip install -r requirements.txt -r requirements-dev.txt
ruff check src/ tests/
pytest tests/unit/test_phase16_preferences.py -v
pytest tests/unit/ tests/contract/ -v          # regression: nothing existing may break
docker compose up postgres redis -d
alembic upgrade head && alembic downgrade -1 && alembic upgrade head
docker exec -it tripplanner_postgres psql -U tripplanner -d tripplanner_db -c "\d user_preferences"
```

- [x] `ruff check src/ tests/` clean
- [x] `test_phase16_preferences.py` passes (56/56 passed)
- [x] existing suite (175 tests before this phase) still passes (240/240 total unit + contract tests passing)
- [ ] `alembic upgrade head` / `downgrade -1` round-trips; CHECK constraint visible in `\d`
- [ ] Manual: `PUT /users/preferences {"dietary_restrictions":["vegetarian"],"home_city":"Delhi"}`, plan a trip,
      `GET /trips/{id}/runs` → `preferences` row shows `state_updates.origin = "DEL"`, `interests` contains `vegetarian`
- [ ] Manual (needs `ANTHROPIC_API_KEY`): plan a 5★ trip for a user with no preferences →
      `GET /users/preferences` shows `travel_style = "luxury"`; `preference_extractor` row in `/runs`
- [ ] Prompt test cases in `prompts/preference_extractor_v1.md` run against the live model; results recorded
- [x] DECISIONS.md updated (append `DECISIONS_phase16_append.md`, entries 37–42)
- [x] README phase table: add row `16 | User Preferences & Personalisation`
