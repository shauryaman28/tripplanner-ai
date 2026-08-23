"""
Unit tests for Phase 11 EvaluatorAgent.

Zero mocks needed for the four check_* functions and evaluate_itinerary()
— they are pure functions (see evaluator.py module docstring for why).
EvaluatorAgent.run() tests mock the DB session only, matching the pattern
in tests/unit/test_run_logger.py.

Covers the roadmap's "4 known-bad itinerary fixtures (one per failure
type)" acceptance criterion plus retry-cap and DB-logging behaviour.
"""

import uuid
from unittest.mock import AsyncMock

import pytest

from src.ai.agents.evaluator import (
    MAX_EVALUATOR_RETRIES,
    EvaluatorAgent,
    EvaluatorFailure,
    EvaluatorVerdict,
    check_activity_dates,
    check_budget_consistency,
    check_duplicate_activities,
    check_hallucinated_activities,
    evaluate_itinerary,
    next_agent_for_failures,
    route_after_evaluation,
)

TRIP_START = "2026-12-10"
TRIP_END = "2026-12-17"

ATTRACTIONS = [
    {"name": "Fort Aguada", "category": "history"},
    {"name": "Baga Beach", "category": "beach"},
    {"name": "Anjuna Flea Market", "category": "shopping"},
]


def _good_draft() -> dict:
    return {
        "days": [
            {
                "day": 1,
                "date": "2026-12-10",
                "morning": {"activity": "Fort Aguada", "cost": 0},
                "afternoon": {"activity": "Baga Beach", "cost": 500},
                "evening": {"activity": "Anjuna Flea Market", "cost": 200},
                "hotel": {"name": "Goa Grand", "cost_per_night": 4500},
                "flight": None,
            }
        ],
        "total_cost": 5200,
        "currency": "INR",
    }


# ── Fixture 1: date_out_of_range ────────────────────────────────────────────


def test_check_activity_dates_catches_out_of_range():
    draft = _good_draft()
    draft["days"][0]["date"] = "2026-12-25"  # outside 12-10..12-17
    failure = check_activity_dates(draft, TRIP_START, TRIP_END)
    assert failure is not None
    assert failure.check == "date_out_of_range"


def test_check_activity_dates_passes_for_valid_range():
    draft = _good_draft()
    assert check_activity_dates(draft, TRIP_START, TRIP_END) is None


# ── Fixture 2: budget_mismatch ──────────────────────────────────────────────


def test_check_budget_consistency_catches_mismatch():
    draft = _good_draft()  # total_cost = 5200
    failure = check_budget_consistency(draft, expected_total=4000)  # 30% off
    assert failure is not None
    assert failure.check == "budget_mismatch"


def test_check_budget_consistency_passes_within_tolerance():
    draft = _good_draft()
    assert check_budget_consistency(draft, expected_total=5300) is None  # ~1.9% off


# ── Fixture 3: duplicate_activity ───────────────────────────────────────────


def test_check_duplicate_activities_catches_dupe():
    draft = _good_draft()
    draft["days"][0]["afternoon"]["activity"] = "Fort Aguada"  # same as morning
    failure = check_duplicate_activities(draft)
    assert failure is not None
    assert failure.check == "duplicate_activity"


def test_check_duplicate_activities_passes_for_unique():
    draft = _good_draft()
    assert check_duplicate_activities(draft) is None


# ── Fixture 4: hallucinated_activity ────────────────────────────────────────


def test_check_hallucinated_activities_catches_hallucination():
    draft = _good_draft()
    draft["days"][0]["evening"]["activity"] = "Fake Museum"  # not in ATTRACTIONS
    failure = check_hallucinated_activities(draft, ATTRACTIONS)
    assert failure is not None
    assert failure.check == "hallucinated_activity"


def test_check_hallucinated_activities_passes_for_known_activities():
    draft = _good_draft()
    assert check_hallucinated_activities(draft, ATTRACTIONS) is None


# ── evaluate_itinerary combined ────────────────────────────────────────────


def test_evaluate_itinerary_good_itinerary_passes_all_checks():
    verdict = evaluate_itinerary(
        _good_draft(), TRIP_START, TRIP_END, expected_budget_total=5200, attractions=ATTRACTIONS
    )
    assert verdict.passed is True
    assert verdict.failures == []


def test_evaluate_itinerary_bad_itinerary_reports_multiple_failures():
    draft = _good_draft()
    draft["days"][0]["date"] = "2026-12-25"
    draft["days"][0]["evening"]["activity"] = "Fake Museum"

    verdict = evaluate_itinerary(
        draft, TRIP_START, TRIP_END, expected_budget_total=5200, attractions=ATTRACTIONS
    )
    assert verdict.passed is False
    checks = {f.check for f in verdict.failures}
    assert "date_out_of_range" in checks
    assert "hallucinated_activity" in checks


# ── Retry routing ────────────────────────────────────────────────────────


def test_next_agent_for_failures_budget_priority():
    failures = [
        EvaluatorFailure(check="duplicate_activity", detail="x"),
        EvaluatorFailure(check="budget_mismatch", detail="y"),
    ]
    assert next_agent_for_failures(failures) == "flight_agent"


def test_next_agent_for_failures_activities_mapping():
    failures = [EvaluatorFailure(check="hallucinated_activity", detail="x")]
    assert next_agent_for_failures(failures) == "activities_agent"


def test_next_agent_for_failures_empty_returns_none():
    assert next_agent_for_failures([]) is None


def test_route_after_evaluation_passed():
    verdict = EvaluatorVerdict(passed=True, failures=[], retry_count=0)
    assert route_after_evaluation(verdict) == "passed"


def test_route_after_evaluation_retry_under_cap():
    verdict = EvaluatorVerdict(
        passed=False, failures=[EvaluatorFailure(check="budget_mismatch", detail="x")], retry_count=1
    )
    assert route_after_evaluation(verdict) == "retry"


def test_route_after_evaluation_failed_at_cap():
    verdict = EvaluatorVerdict(
        passed=False,
        failures=[EvaluatorFailure(check="budget_mismatch", detail="x")],
        retry_count=MAX_EVALUATOR_RETRIES,
    )
    assert route_after_evaluation(verdict) == "failed"


# ── EvaluatorAgent.run() — DB logging ───────────────────────────────────────


@pytest.mark.asyncio
async def test_evaluator_agent_run_logs_agent_run_completed():
    mock_session = AsyncMock()
    added = []
    mock_session.add = lambda obj: added.append(obj)
    mock_session.commit = AsyncMock()
    mock_session.refresh = AsyncMock()

    agent = EvaluatorAgent()
    verdict = await agent.run(
        draft=_good_draft(),
        trip_start=TRIP_START,
        trip_end=TRIP_END,
        expected_budget_total=5200,
        attractions=ATTRACTIONS,
        db=mock_session,
        trip_id=uuid.uuid4(),
    )

    assert verdict.passed is True
    assert len(added) == 1
    row = added[0]
    assert row.agent_name == "evaluator"
    assert row.status == "completed"
    assert row.output["passed"] is True


@pytest.mark.asyncio
async def test_evaluator_agent_run_logs_agent_run_failed():
    mock_session = AsyncMock()
    added = []
    mock_session.add = lambda obj: added.append(obj)
    mock_session.commit = AsyncMock()
    mock_session.refresh = AsyncMock()

    bad_draft = _good_draft()
    bad_draft["days"][0]["date"] = "2026-12-25"

    agent = EvaluatorAgent()
    verdict = await agent.run(
        draft=bad_draft,
        trip_start=TRIP_START,
        trip_end=TRIP_END,
        expected_budget_total=5200,
        attractions=ATTRACTIONS,
        retry_count=1,
        db=mock_session,
        trip_id=uuid.uuid4(),
    )

    assert verdict.passed is False
    assert verdict.retry_count == 1
    row = added[0]
    assert row.status == "failed"
