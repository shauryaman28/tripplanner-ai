"""
Unit tests for Phase 16 — User preferences & personalisation.

Zero network, zero Docker. The LLM seam (`_call_llm`), the sub-agents and the
DB session are mocked, matching the patterns in test_phase13/15.

Dev A — injection + extractor
  - pure injection logic (dietary → interests, home_city → origin, style defaults)
  - apply_preferences_node → FlightAgent / ActivitiesAgent receive the preferences
  - preferred_airlines forwarded by FlightAgent and honoured by the MCP tool
  - PreferenceExtractor: LLM path, LLM-failure fallback, additive-only writes

Dev B — endpoints
  - GET /users/preferences (empty defaults + saved row)
  - PUT /users/preferences (create, full overwrite, validation, auth)
"""

import json
import uuid
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.preferences import merge_unique, normalise_airlines, normalise_dietary
from app.models.agent_run import AgentRun
from app.models.trip import Trip
from app.models.user_preferences import UserPreferences
from src.ai.agents.flight_agent import search_flights_node
from src.ai.agents.preference_extractor import (
    PreferenceExtractor,
    build_trip_facts,
    extract_preferences,
    infer_travel_style,
)
from src.ai.builder.builder import _build_user_prompt
from src.ai.mcp_server.models import FlightSearchInput
from src.ai.mcp_server.tools import search_flights
from src.ai.orchestrator.orchestrator import (
    apply_preferences_node,
    build_orchestrator_graph,
    extract_preferences_node,
    hotel_activities_node,
    run_flight_node,
)
from src.ai.utils.preferences import (
    build_preference_updates,
    merge_extracted,
    preferred_airlines_from,
    resolve_origin_code,
)

FUTURE = (date.today() + timedelta(days=30)).isoformat()
_LLM = "src.ai.agents.preference_extractor._call_llm"

_FLIGHT = {
    "airline": "6E", "flight_number": "6E-204",
    "departure": "2026-12-10T06:00:00", "arrival": "2026-12-10T08:15:00",
    "duration_mins": 135, "price_inr": 8200.0, "stops": 0,
}

_LUX_STATE = {
    "destination": "Goa",
    "group_size": 2,
    "interests": ["beach"],
    "raw_input": "We are vegetarian and prefer Air India",
    "hotels": [{"name": "Taj", "stars": 5, "price_per_night_inr": 12_000.0}],
    "draft_itinerary": {
        "days": [
            {
                "day": 1, "date": "2026-12-10",
                "morning": {"activity": "Fort Aguada", "cost": 0},
                "afternoon": {"activity": "Explore the area", "cost": 0},
                "evening": None,
                "hotel": {"name": "Taj", "cost_per_night": 12_000.0},
                "flight": None,
            },
            {
                "day": 2, "date": "2026-12-11",
                "morning": {"activity": "Baga Beach", "cost": 500},
                "afternoon": None, "evening": None,
                "hotel": {"name": "Taj", "cost_per_night": 12_000.0},
                "flight": None,
            },
        ],
        "total_cost": 60_000.0,
        "currency": "INR",
    },
}


# ── Helpers ────────────────────────────────────────────────────────────────


def _prefs(**kwargs) -> UserPreferences:
    return UserPreferences(user_id=uuid.uuid4(), **kwargs)


def _mock_db(trip=None, prefs=None) -> AsyncMock:
    """Session whose db.get() serves a Trip or a UserPreferences by model class."""
    db = AsyncMock()
    added: list = []

    async def _get(model, _pk):
        return trip if model is Trip else prefs

    db.get = _get
    db.add = lambda obj: added.append(obj)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.rollback = AsyncMock()
    db._added = added
    return db


def _trip_for(user_id: uuid.UUID) -> MagicMock:
    trip = MagicMock()
    trip.user_id = user_id
    return trip


# ── core helpers (normalise / merge) ───────────────────────────────────────


def test_normalise_dietary_lowercases_and_dedupes():
    assert normalise_dietary([" Vegetarian ", "vegetarian", "", "VEGAN"]) == ["vegetarian", "vegan"]


def test_normalise_airlines_uppercases_and_drops_invalid_codes():
    assert normalise_airlines(["6e", "ai", "IndiGo", "AI"]) == ["6E", "AI"]


def test_normalise_helpers_tolerate_non_list_input():
    assert normalise_dietary(None) == []
    assert normalise_airlines("6E") == []


def test_merge_unique_is_case_insensitive_and_first_spelling_wins():
    assert merge_unique(["Vegan"], ["vegan", "jain"]) == ["Vegan", "jain"]


def test_merge_unique_limit_never_truncates_existing():
    assert merge_unique(["a", "b", "c"], ["d"], limit=2) == ["a", "b", "c"]


# ── injection (pure) ───────────────────────────────────────────────────────


def test_dietary_restrictions_are_appended_to_interests():
    updates = build_preference_updates({"interests": ["beach"]}, {"dietary_restrictions": ["vegetarian"]})
    assert updates["interests"] == ["beach", "vegetarian"]


def test_interests_untouched_when_dietary_already_present():
    updates = build_preference_updates({"interests": ["Vegetarian"]}, {"dietary_restrictions": ["vegetarian"]})
    assert "interests" not in updates


def test_style_default_interests_only_apply_without_explicit_interests():
    bare = build_preference_updates({"interests": []}, {"travel_style": "luxury"})
    assert bare["interests"] == ["wellness"]

    explicit = build_preference_updates({"interests": ["history"]}, {"travel_style": "luxury"})
    assert "interests" not in explicit


def test_home_city_sets_origin_only_when_missing():
    assert build_preference_updates({}, {"home_city": "Delhi"})["origin"] == "DEL"
    assert "origin" not in build_preference_updates({"origin": "BOM"}, {"home_city": "Delhi"})


def test_build_preference_updates_is_idempotent():
    prefs = {"dietary_restrictions": ["vegetarian"], "home_city": "Delhi"}
    first = build_preference_updates({"interests": []}, prefs)
    second = build_preference_updates({"interests": [], **first}, prefs)
    assert set(second) == {"preferences"}


@pytest.mark.parametrize(
    "home_city, expected",
    [("Delhi", "DEL"), ("Goa", "GOI"), ("blr", "BLR"), ("  Mumbai ", "BOM"), ("Atlantis", None), ("", None), (None, None)],
)
def test_resolve_origin_code(home_city, expected):
    assert resolve_origin_code(home_city) == expected


def test_preferred_airlines_from_state():
    assert preferred_airlines_from({}) == []
    assert preferred_airlines_from({"preferences": {"preferred_airlines": ["6E"]}}) == ["6E"]


# ── additive merge (pure) ──────────────────────────────────────────────────


def test_merge_extracted_unions_lists_and_fills_unset_scalars():
    existing = {"dietary_restrictions": ["vegan"], "preferred_airlines": [], "travel_style": None, "home_city": None}
    extracted = {"dietary_restrictions": ["jain"], "preferred_airlines": ["AI"], "travel_style": "luxury", "home_city": "Pune"}
    assert merge_extracted(existing, extracted) == {
        "dietary_restrictions": ["vegan", "jain"],
        "preferred_airlines": ["AI"],
        "travel_style": "luxury",
        "home_city": "Pune",
    }


def test_merge_extracted_never_overwrites_set_scalars_or_drops_items():
    existing = {"dietary_restrictions": ["vegan"], "preferred_airlines": ["6E"], "travel_style": "budget", "home_city": "Pune"}
    extracted = {"dietary_restrictions": [], "preferred_airlines": [], "travel_style": "luxury", "home_city": "Delhi"}
    assert merge_extracted(existing, extracted) == {}


# ── travel_style heuristic + facts ─────────────────────────────────────────


@pytest.mark.parametrize(
    "stars, ppn, expected",
    [
        (5.0, 15_000, "luxury"),
        (2.0, 1_500, "budget"),
        (3.0, 5_000, "mid-range"),
        (5.0, 2_000, "mid-range"),  # signals disagree → rounds down
        (2.0, 5_000, "budget"),
        (None, 10_000, "luxury"),
        (4.0, None, "mid-range"),
        (None, None, None),
    ],
)
def test_infer_travel_style(stars, ppn, expected):
    assert infer_travel_style(stars, ppn) == expected


def test_build_trip_facts_joins_hotel_stars_and_computes_per_person_spend():
    facts = build_trip_facts(_LUX_STATE)
    assert facts["nights"] == 2
    assert facts["avg_hotel_stars"] == 5.0
    assert facts["per_person_per_night_inr"] == 15_000
    assert facts["hotels"] == ["Taj"]
    assert facts["top_activities"] == ["Fort Aguada", "Baga Beach"]  # fallback phrase excluded


# ── extract_preferences (LLM seam mocked) ──────────────────────────────────


@pytest.mark.asyncio
async def test_extract_preferences_coerces_llm_output():
    reply = json.dumps({
        "travel_style": "posh",  # invalid → dropped → heuristic fills in
        "dietary_restrictions": ["Vegetarian", "vegetarian"],
        "preferred_airlines": ["ai", "IndiGo"],  # "IndiGo" is not a carrier code → dropped
        "home_city": "  Pune ",
    })
    with patch(_LLM, AsyncMock(return_value=reply)):
        result = await extract_preferences(build_trip_facts(_LUX_STATE))

    assert result == {
        "travel_style": "luxury",
        "dietary_restrictions": ["vegetarian"],
        "preferred_airlines": ["AI"],
        "home_city": "Pune",
    }


@pytest.mark.asyncio
async def test_extract_preferences_accepts_fenced_json():
    reply = '```json\n{"travel_style": "budget", "dietary_restrictions": [], "preferred_airlines": [], "home_city": null}\n```'
    with patch(_LLM, AsyncMock(return_value=reply)):
        result = await extract_preferences(build_trip_facts(_LUX_STATE))
    assert result["travel_style"] == "budget"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [AsyncMock(side_effect=RuntimeError("no key")), AsyncMock(return_value="not json")])
async def test_extract_preferences_falls_back_to_heuristic_style(failure):
    with patch(_LLM, failure):
        result = await extract_preferences(build_trip_facts(_LUX_STATE))
    assert result == {"travel_style": "luxury"}


# ── PreferenceExtractor.run (additive writes + logging) ────────────────────

_LLM_LUX = json.dumps({
    "travel_style": "luxury",
    "dietary_restrictions": ["Vegetarian"],
    "preferred_airlines": ["ai"],
    "home_city": None,
})


@pytest.mark.asyncio
async def test_extractor_creates_row_and_updates_travel_style_for_new_user():
    db = _mock_db()
    with patch(_LLM, AsyncMock(return_value=_LLM_LUX)):
        changes = await PreferenceExtractor().run(_LUX_STATE, db=db, user_id=uuid.uuid4(), trip_id=uuid.uuid4())

    rows = [o for o in db._added if isinstance(o, UserPreferences)]
    assert len(rows) == 1
    assert rows[0].travel_style == "luxury"
    assert rows[0].dietary_restrictions == ["vegetarian"]
    assert rows[0].preferred_airlines == ["AI"]
    assert changes == {
        "dietary_restrictions": ["vegetarian"],
        "preferred_airlines": ["AI"],
        "travel_style": "luxury",
    }


@pytest.mark.asyncio
async def test_extractor_is_additive_and_never_overwrites_explicit_preferences():
    existing = _prefs(dietary_restrictions=["vegan"], travel_style="budget", home_city="Pune")
    db = _mock_db(prefs=existing)
    reply = json.dumps({
        "travel_style": "luxury", "dietary_restrictions": ["jain"], "preferred_airlines": [], "home_city": "Delhi",
    })
    with patch(_LLM, AsyncMock(return_value=reply)):
        changes = await PreferenceExtractor().run(_LUX_STATE, db=db, user_id=existing.user_id, trip_id=uuid.uuid4())

    assert existing.dietary_restrictions == ["vegan", "jain"]  # union, existing first
    assert existing.travel_style == "budget"  # already set → untouched
    assert existing.home_city == "Pune"
    assert changes == {"dietary_restrictions": ["vegan", "jain"]}


@pytest.mark.asyncio
async def test_extractor_writes_nothing_when_nothing_changes():
    existing = _prefs(travel_style="luxury")
    db = _mock_db(prefs=existing)
    reply = json.dumps({"travel_style": "luxury", "dietary_restrictions": [], "preferred_airlines": [], "home_city": None})
    with patch(_LLM, AsyncMock(return_value=reply)):
        changes = await PreferenceExtractor().run(_LUX_STATE, db=db, user_id=existing.user_id, trip_id=uuid.uuid4())

    assert changes == {}
    assert not [o for o in db._added if isinstance(o, UserPreferences)]


@pytest.mark.asyncio
async def test_extractor_logs_one_agent_run_row():
    db = _mock_db()
    with patch(_LLM, AsyncMock(return_value=_LLM_LUX)):
        await PreferenceExtractor().run(_LUX_STATE, db=db, user_id=uuid.uuid4(), trip_id=uuid.uuid4(), turn=2)

    runs = [o for o in db._added if isinstance(o, AgentRun)]
    assert len(runs) == 1
    assert runs[0].agent_name == "preference_extractor"
    assert runs[0].status == "completed"
    assert runs[0].turn == 2
    assert runs[0].output["applied"]["travel_style"] == "luxury"
    assert runs[0].duration_ms is not None


# ── orchestrator nodes ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_saved_preferences_reach_flight_and_activities_agents():
    """Phase 16 acceptance: vegetarian lands in the ActivitiesAgent interests and
    home_city=Delhi becomes origin=DEL — without the user asking for either."""
    user_id, trip_id = uuid.uuid4(), uuid.uuid4()
    prefs = UserPreferences(
        user_id=user_id, dietary_restrictions=["vegetarian"], preferred_airlines=["6E"], home_city="Delhi"
    )
    db = _mock_db(trip=_trip_for(user_id), prefs=prefs)
    state = {
        "destination": "Goa", "start_date": "2026-12-10", "end_date": "2026-12-17",
        "budget": 50_000.0, "group_size": 2, "interests": [],
        "db": db, "trip_id": trip_id, "publish_fn": None,
    }

    state = await apply_preferences_node(state)
    assert state["origin"] == "DEL"
    assert state["interests"] == ["vegetarian"]

    with (
        patch("src.ai.orchestrator.orchestrator.FlightAgent") as MockFA,
        patch("src.ai.orchestrator.orchestrator.HotelAgent") as MockHA,
        patch("src.ai.orchestrator.orchestrator.ActivitiesAgent") as MockAA,
    ):
        MockFA.return_value.run = AsyncMock(return_value={"flights": [_FLIGHT], "error": None})
        MockHA.return_value.run = AsyncMock(return_value={"hotels": [], "error": None})
        MockAA.return_value.run = AsyncMock(return_value={"attractions": [], "error": None})

        state = await run_flight_node(state)
        await hotel_activities_node(state)

    flight_input = MockFA.return_value.run.await_args.args[0]
    assert flight_input["origin"] == "DEL"
    assert flight_input["preferred_airlines"] == ["6E"]
    assert "vegetarian" in MockAA.return_value.run.await_args.args[0]["interests"]

    logged = [o for o in db._added if isinstance(o, AgentRun)]
    assert [r.agent_name for r in logged] == ["preferences"]
    assert logged[0].output["loaded"] is True


@pytest.mark.asyncio
async def test_apply_preferences_node_without_saved_row_leaves_state_alone():
    user_id = uuid.uuid4()
    db = _mock_db(trip=_trip_for(user_id), prefs=None)
    state = {"interests": ["beach"], "db": db, "trip_id": uuid.uuid4()}

    result = await apply_preferences_node(state)

    assert result["interests"] == ["beach"]
    assert "origin" not in result
    assert "preferences" not in result
    logged = [o for o in db._added if isinstance(o, AgentRun)]
    assert logged[0].output["loaded"] is False


@pytest.mark.asyncio
async def test_apply_preferences_node_is_a_noop_without_db_or_resolvable_user():
    assert await apply_preferences_node({"interests": ["x"]}) == {"interests": ["x"]}

    unresolvable = _mock_db(trip=MagicMock(), prefs=None)  # trip.user_id is not a UUID
    state = {"interests": ["x"], "db": unresolvable, "trip_id": uuid.uuid4()}
    assert await apply_preferences_node(state) == state
    assert unresolvable._added == []


@pytest.mark.asyncio
async def test_extract_preferences_node_is_a_noop_without_db():
    with patch(_LLM, AsyncMock()) as llm:
        result = await extract_preferences_node({"draft_itinerary": _LUX_STATE["draft_itinerary"]})
    llm.assert_not_awaited()
    assert result == {"draft_itinerary": _LUX_STATE["draft_itinerary"]}


@pytest.mark.asyncio
async def test_extract_preferences_node_swallows_failures_and_rolls_back():
    user_id = uuid.uuid4()
    db = _mock_db(trip=_trip_for(user_id))
    state = {**_LUX_STATE, "db": db, "trip_id": uuid.uuid4()}

    with patch("src.ai.orchestrator.orchestrator.PreferenceExtractor") as MockPE:
        MockPE.return_value.run = AsyncMock(side_effect=RuntimeError("boom"))
        result = await extract_preferences_node(state)

    assert result is state  # planning result unaffected
    db.rollback.assert_awaited_once()


def test_orchestrator_graph_contains_the_phase16_nodes():
    nodes = set(build_orchestrator_graph().get_graph().nodes)
    assert {"apply_preferences", "extract_preferences"} <= nodes


# ── FlightAgent + MCP tool: preferred_airlines ─────────────────────────────


@pytest.mark.asyncio
async def test_flight_node_forwards_preferred_airlines_only_when_present():
    base = {"origin": "DEL", "destination": "GOI", "date": FUTURE, "budget": 20_000, "passengers": 1}
    mock_call = AsyncMock(return_value=[])
    with patch("src.ai.agents.flight_agent.call_tool", mock_call):
        await search_flights_node({**base, "preferred_airlines": ["AI"]})
        await search_flights_node({**base, "preferred_airlines": []})

    with_pref, without_pref = (c.args[1] for c in mock_call.await_args_list)
    assert with_pref["preferred_airlines"] == ["AI"]
    assert "preferred_airlines" not in without_pref


def _offer(carrier: str, number: str, price: str) -> dict:
    return {
        "itineraries": [{
            "segments": [{
                "carrierCode": carrier, "number": number,
                "departure": {"at": f"{FUTURE}T06:00:00"}, "arrival": {"at": f"{FUTURE}T08:15:00"},
            }],
            "duration": "PT2H15M",
        }],
        "price": {"grandTotal": price},
    }


def _search(preferred):
    response = MagicMock()
    response.data = [_offer("6E", "204", "4200.00"), _offer("AI", "805", "5100.00"), _offer("UK", "995", "4800.00")]
    settings = MagicMock()
    settings.AMADEUS_CLIENT_ID = "id"
    settings.AMADEUS_CLIENT_SECRET = "secret"
    with (
        patch("src.ai.mcp_server.tools.mcp_settings", settings),
        patch("src.ai.mcp_server.tools.get_cached_sync", return_value=None),
        patch("src.ai.mcp_server.tools.set_cached_sync"),
        patch("src.ai.mcp_server.tools.AmadeusClient") as MockClient,
    ):
        MockClient.return_value.shopping.flight_offers_search.get.return_value = response
        result = search_flights(
            FlightSearchInput(
                origin="DEL", destination="GOI", date=FUTURE, budget=20_000, passengers=1, preferred_airlines=preferred
            )
        )
    return [f.airline for f in result]


def test_search_flights_ranks_preferred_carriers_first_without_dropping_others():
    assert _search(None) == ["6E", "AI", "UK"]
    assert _search(["ai"]) == ["AI", "6E", "UK"]  # case-insensitive, others kept
    assert _search(["UK", "AI"]) == ["AI", "UK", "6E"]  # stable within the preferred group


# ── ItineraryBuilder prompt ────────────────────────────────────────────────

_META = {"destination": "Goa", "start_date": "2026-12-10", "end_date": "2026-12-11", "group_size": 2}


def test_builder_prompt_includes_preferences_block_with_data_scope_guard():
    meta = {
        **_META,
        "preferences": {
            "dietary_restrictions": ["vegetarian"], "preferred_airlines": [], "travel_style": "luxury", "home_city": None,
        },
    }
    prompt = _build_user_prompt(meta, [], [], [])
    assert "vegetarian" in prompt
    assert "luxury" in prompt
    assert "never introduce a name" in prompt


def test_builder_prompt_unchanged_without_preferences():
    assert "Traveller preferences" not in _build_user_prompt(_META, [], [], [])
    empty = {**_META, "preferences": {"dietary_restrictions": [], "preferred_airlines": [], "travel_style": None, "home_city": None}}
    assert "Traveller preferences" not in _build_user_prompt(empty, [], [], [])


# ── Routes: GET/PUT /users/preferences ─────────────────────────────────────


def _route_session(prefs=None) -> AsyncMock:
    session = AsyncMock()
    session.get = AsyncMock(return_value=prefs)
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    return session


async def _call(method: str, session: AsyncMock, body: dict | None = None):
    from app.api.deps import get_current_user
    from app.db.session import get_db
    from app.main import app

    user = MagicMock()
    user.id = uuid.uuid4()

    async def _db():
        yield session

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = _db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.request(method, "/users/preferences", json=body)
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_get_preferences_returns_empty_defaults_for_new_user():
    resp = await _call("GET", _route_session(prefs=None))
    assert resp.status_code == 200
    assert resp.json() == {
        "dietary_restrictions": [], "preferred_airlines": [], "travel_style": None, "home_city": None, "updated_at": None,
    }


@pytest.mark.asyncio
async def test_get_preferences_returns_saved_row():
    saved = _prefs(dietary_restrictions=["vegetarian"], travel_style="mid-range", home_city="Delhi")
    resp = await _call("GET", _route_session(prefs=saved))
    data = resp.json()
    assert resp.status_code == 200
    assert data["dietary_restrictions"] == ["vegetarian"]
    assert data["travel_style"] == "mid-range"
    assert data["home_city"] == "Delhi"
    assert data["updated_at"] is not None


@pytest.mark.asyncio
async def test_put_preferences_creates_row_and_canonicalises_input():
    session = _route_session(prefs=None)
    resp = await _call(
        "PUT",
        session,
        {
            "dietary_restrictions": [" Vegetarian ", "vegetarian"],
            "preferred_airlines": ["6e"],
            "travel_style": "luxury",
            "home_city": " Delhi ",
        },
    )
    data = resp.json()
    assert resp.status_code == 200
    assert data["dietary_restrictions"] == ["vegetarian"]
    assert data["preferred_airlines"] == ["6E"]
    assert data["home_city"] == "Delhi"
    session.add.assert_called_once()
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_put_preferences_is_a_full_overwrite():
    existing = _prefs(
        dietary_restrictions=["vegan"], preferred_airlines=["AI"], travel_style="budget", home_city="Pune"
    )
    resp = await _call("PUT", _route_session(prefs=existing), {"travel_style": "luxury"})
    data = resp.json()

    assert resp.status_code == 200
    assert data["travel_style"] == "luxury"
    assert data["dietary_restrictions"] == []  # omitted → reset, not preserved
    assert data["preferred_airlines"] == []
    assert data["home_city"] is None
    assert existing.dietary_restrictions == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"preferred_airlines": ["INDIGO"]},  # not a 2-char IATA code
        {"travel_style": "posh"},  # not an allowed style
        {"dietary_restrictions": [f"diet{i}" for i in range(21)]},  # > MAX_LIST_ITEMS
        {"home_city": "x" * 101},
    ],
)
async def test_put_preferences_rejects_invalid_bodies(body):
    session = _route_session()
    resp = await _call("PUT", session, body)
    assert resp.status_code == 422
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_preferences_endpoints_require_auth():
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/users/preferences")).status_code == 401
        assert (await client.put("/users/preferences", json={})).status_code == 401
