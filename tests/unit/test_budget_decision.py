"""
Unit tests for Phase 10 budget decision pure function.

All tests call make_budget_decision() directly — no mocks, no network.
"""

import pytest

from src.ai.agents.budget_decision import (
    MAX_REPLAN_ATTEMPTS,
    make_budget_decision,
    replan_flight_budget,
)


def _flights(price: float) -> list[dict]:
    return [{"price_inr": price}]


# ── make_budget_decision ────────────────────────────────────────────────────


def test_70_percent_flights_escalates():
    """₹28k of ₹40k budget (70%) → 30% remaining < 35% threshold → escalate."""
    result = make_budget_decision(_flights(28_000), total_budget=40_000)
    assert result.decision == "escalate"
    assert result.remaining_budget == 12_000.0
    assert result.flight_cost == 28_000.0


def test_40_percent_flights_continues():
    """₹16k of ₹40k budget (40%) → 60% remaining ≥ 50% threshold → continue."""
    result = make_budget_decision(_flights(16_000), total_budget=40_000)
    assert result.decision == "continue"
    assert result.remaining_budget == 24_000.0


def test_55_percent_flights_replans():
    """₹22k of ₹40k budget (55%) → 45% remaining → 35% < 45% < 50% → replan."""
    result = make_budget_decision(_flights(22_000), total_budget=40_000)
    assert result.decision == "replan"
    assert result.remaining_budget == 18_000.0


def test_max_replan_attempts_forces_escalate():
    """Even borderline budget: cap hit → escalate regardless of remaining fraction."""
    # 45% remaining would normally replan, but attempt cap overrides
    result = make_budget_decision(
        _flights(22_000), total_budget=40_000, replan_attempts=MAX_REPLAN_ATTEMPTS
    )
    assert result.decision == "escalate"
    assert "Maximum re-planning" in result.reason


def test_empty_flights_escalates():
    """No flights returned → escalate."""
    result = make_budget_decision([], total_budget=40_000)
    assert result.decision == "escalate"
    assert result.flight_cost == 0.0


def test_zero_budget_escalates():
    """Zero budget → escalate."""
    result = make_budget_decision(_flights(5_000), total_budget=0)
    assert result.decision == "escalate"


def test_exactly_50_percent_remaining_continues():
    """Exactly 50% remaining → continue (boundary: inclusive lower bound of 'continue')."""
    result = make_budget_decision(_flights(20_000), total_budget=40_000)
    assert result.decision == "continue"


def test_decision_carries_correct_numerical_fields():
    """BudgetDecision has correct flight_cost, remaining_budget, total_budget."""
    result = make_budget_decision(_flights(8_200), total_budget=50_000)
    assert result.flight_cost == 8_200.0
    assert result.remaining_budget == 41_800.0
    assert result.total_budget == 50_000.0


# ── replan_flight_budget ────────────────────────────────────────────────────


def test_replan_attempt_1_reduces_budget_to_65_percent():
    assert replan_flight_budget(50_000, attempt=1) == 50_000 * 0.65


def test_replan_attempt_2_reduces_budget_further():
    b1 = replan_flight_budget(50_000, attempt=1)
    b2 = replan_flight_budget(50_000, attempt=2)
    assert b2 < b1
    assert b2 == 50_000 * 0.55
