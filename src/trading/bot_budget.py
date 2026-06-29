"""Deployable bankroll and remaining budget helpers for hourly / 15m bots."""

from __future__ import annotations

from typing import Protocol


class BudgetSettings(Protocol):
  mode: str
  use_accumulated_profit: bool
  profit_use_pct: float


def _clamp_profit_use_pct(pct: float) -> float:
  return max(0.0, min(100.0, float(pct)))


def _realized_adjustment(
  realized: float,
  *,
  use_accumulated_profit: bool,
  profit_use_pct: float,
) -> float:
  if not use_accumulated_profit:
    return min(0.0, realized)
  if realized <= 0:
    return realized
  pct = _clamp_profit_use_pct(profit_use_pct) / 100.0
  return realized * pct


def deploy_bankroll_usd(
  *,
  mode: str,
  use_accumulated_profit: bool,
  profit_use_pct: float = 100.0,
  max_cap: float,
  paper_bankroll_usd: float,
  interval_realized_pnl_usd: float,
) -> float:
  """Capital the bot may use for new entries this interval."""
  pct = _clamp_profit_use_pct(profit_use_pct) / 100.0
  if mode == "paper":
    if use_accumulated_profit:
      paper = max(0.0, float(paper_bankroll_usd))
      cap = float(max_cap)
      if paper <= cap:
        return paper
      return cap + (paper - cap) * pct
    return max(0.0, min(float(paper_bankroll_usd), float(max_cap)))
  realized = float(interval_realized_pnl_usd)
  adjustment = _realized_adjustment(
    realized,
    use_accumulated_profit=use_accumulated_profit,
    profit_use_pct=profit_use_pct,
  )
  return max(0.0, float(max_cap) + adjustment)


def remaining_budget_usd(
  *,
  settings: BudgetSettings,
  max_cap: float,
  paper_bankroll_usd: float,
  interval_realized_pnl_usd: float,
  open_exposure_usd: float,
  interval_total_entered_usd: float,
) -> float:
  """Budget left for new entries after open exposure and optional interval cap."""
  deploy = deploy_bankroll_usd(
    mode=settings.mode,
    use_accumulated_profit=settings.use_accumulated_profit,
    profit_use_pct=settings.profit_use_pct,
    max_cap=max_cap,
    paper_bankroll_usd=paper_bankroll_usd,
    interval_realized_pnl_usd=interval_realized_pnl_usd,
  )
  concurrent_room = max(0.0, min(deploy, float(max_cap)) - open_exposure_usd)
  if settings.use_accumulated_profit:
    return concurrent_room
  interval_room = max(0.0, float(max_cap) - float(interval_total_entered_usd))
  return min(concurrent_room, interval_room)
