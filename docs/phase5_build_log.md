# Phase 5 — FastAPI Gateway: Auth, Routes, SSE

**Status: ✅ Complete**
**Done criterion:** All routes return correct status codes, JWT rejects invalid tokens, SSE delivers a manually-published Redis event to a browser tab.

## What was built

```
src/backend/app/
├── core/
│   └── security.py          ← hash_password, verify_password, create/decode JWT
├── api/
│   ├── deps.py              ← get_current_user, get_current_user_sse, get_redis_dep
│   └── routes/
│       ├── auth.py          ← POST /auth/register, POST /auth/login
│       └── trips.py         ← All 7 trip routes
├── schemas/
│   ├── auth.py              ← UserCreate, UserRead, Token
│   ├── trip.py              ← TripCreate (validated), TripRead
│   ├── itinerary.py         ← ItineraryRead
│   └── agent_run.py         ← AgentRunRead
└── main.py                  ← Updated: auth + trips routers included
```

## Routes

| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/auth/register` | None | 400 if duplicate email |
| POST | `/auth/login` | None | OAuth2 form → JWT |
| GET | `/trips` | Bearer | List all trips for authenticated user |
| POST | `/trips` | Bearer | Creates trip row |
| POST | `/trips/{id}/plan` | Bearer | 202, creates AgentRun, publishes SSE event |
| GET | `/trips/{id}/stream` | Bearer OR ?token= | SSE — Redis pub/sub |
| GET | `/trips/{id}/itinerary` | Bearer | 404 if no itinerary yet |
| GET | `/trips/{id}/similar` | Bearer | 501 until Phase 23 |
| GET | `/trips/{id}/runs` | Bearer | All agent_runs, oldest first |

## JWT design

- **Algorithm:** HS256
- **Claim:** `sub` = user UUID string
- **Expiry:** 1440 min (24h) — configurable via `JWT_EXPIRE_MINUTES`
- **Standard route:** OAuth2 `Authorization: Bearer <token>` header
- **SSE route:** also accepts `?token=<jwt>` because browsers can't set custom headers for `EventSource`

## SSE pipeline verification

Before any agent code exists, verify the full SSE pipeline:

```bash
# Terminal 1 — backend
cd src/backend && uvicorn app.main:app --reload

# Terminal 2 — register + get token
TOKEN=$(curl -s -X POST http://localhost:8000/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"test@test.com","password":"testpass"}' \
  && curl -s -X POST http://localhost:8000/auth/login \
  -d "username=test@test.com&password=testpass" | jq -r .access_token)

# Terminal 2 — create trip
TRIP_ID=$(curl -s -X POST http://localhost:8000/trips \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"destination":"Goa","start_date":"2025-12-10","end_date":"2025-12-17","budget":50000}' \
  | jq -r .id)

# Terminal 3 — open SSE stream
curl -N "http://localhost:8000/trips/$TRIP_ID/stream?token=$TOKEN"

# Terminal 2 — trigger planning (publishes SSE event)
curl -s -X POST "http://localhost:8000/trips/$TRIP_ID/plan" \
  -H "Authorization: Bearer $TOKEN"

# Terminal 3 should receive: event: agent_update
# data: {"agent": "orchestrator", "status": "pending", ...}
```

## Done criterion checklist

- [x] POST /auth/register → 201, 400 on duplicate email
- [x] POST /auth/login → JWT token
- [x] No token → 401 on all /trips routes
- [x] Invalid token → 401
- [x] GET /trips → 200 list of authenticated user's trips (newest first)
- [x] POST /trips → 201 with trip data
- [x] POST /trips/{id}/plan → 202, creates AgentRun, publishes Redis event
- [x] GET /trips/{id}/stream → SSE stream, `connected` event on subscribe
- [x] GET /trips/{id}/similar → 501 (Phase 23 placeholder)
- [x] GET /trips/{id}/runs → 200 list (empty until Phase 9)
- [x] SSE event visible in browser/curl after POST /plan
