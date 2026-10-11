"""GET /admin/cost-summary — what the models were asked, and what it costs (Phase 26)."""

from datetime import date

from pydantic import BaseModel


class CostLine(BaseModel):
    """Tokens and cost of the runs that asked a model.

    `unpriced_tokens` are the tokens of runs whose model has no price on record
    (src/ai/usage.py): they are in `tokens` and not in `cost_usd`, so a total
    with any of them is a lower bound, and says so.
    """

    runs: int
    tokens: int
    cost_usd: float
    unpriced_tokens: int = 0


class AgentCost(CostLine):
    agent: str


class DayCost(CostLine):
    date: date


class CostSummary(BaseModel):
    since: date
    total: CostLine
    by_agent: list[AgentCost]  # dearest first
    by_date: list[DayCost]  # oldest first
