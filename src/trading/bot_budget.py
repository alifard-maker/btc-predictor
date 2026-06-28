"""Deployable bankroll and remaining budget helpers for hourly / 15m bots."""

from __future__ import annotations

from typing import Any, Protocol


class BudgetSettings(Protocol):
  mode: str
  use_accumulated_profit: bool


def deploy_bankroll_usd(
  *,
  mode: str,
  use_accumulated_profit: bool,
  max_cap: float,
  paper_bankroll_usd: float,
  interval_realized_pnl_usd: float,
) -> float:
  """Capital the bot may use for new entries this interval."""
  if mode == "paper":
    if use_accumulated_profit:
      return max(0.0, paper_bankroll_usd)
    return max(0.0, min(paper_bankroll_usd, float(max_cap)))
  realized = float(interval_realized_pnl_usd)
  if use_accumulated_profit:
    return max(0.0, float(max_cap) + realized)
  return max(0.0, float(max_cap) + min(0.0, realized))


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
    max_cap=max_cap,
    paper_bankroll_usd=paper_bankroll_usd,
    interval_realized_pnl_usd=interval_realized_pnl_usd,
  )
  concurrent_room = max(0.0, min(deploy, float(max_cap)) - open_exposure_usd)
  if settings.use_accumulated_profit:
    return concurrent_room
  interval_room = max(0.0, float(max_cap) - float(interval_total_entered_usd))
  return min(concurrent_room, interval_room)
