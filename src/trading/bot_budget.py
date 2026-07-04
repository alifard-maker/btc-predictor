"""Deployable bankroll and remaining budget helpers for hourly / 15m bots."""

from __future__ import annotations

from typing import Protocol


class BudgetSettings(Protocol):
  mode: str
  use_accumulated_profit: bool
  profit_use_pct: float
  live_auto_refill_hour_budget: bool


def _live_auto_refill_enabled(settings: BudgetSettings) -> bool:
  return str(settings.mode).lower() == "live" and bool(
    getattr(settings, "live_auto_refill_hour_budget", False)
  )


def live_win_cap_boost(settings: BudgetSettings, interval_realized_pnl_usd: float) -> float:
  """Extra hour cap from realized wins when live auto-refill is enabled."""
  if not _live_auto_refill_enabled(settings):
    return 0.0
  realized = float(interval_realized_pnl_usd)
  if realized <= 0:
    return 0.0
  if settings.use_accumulated_profit:
    pct = _clamp_profit_use_pct(settings.profit_use_pct) / 100.0
    return realized * pct
  return realized


def live_effective_hour_cap(
  max_cap: float,
  settings: BudgetSettings,
  interval_realized_pnl_usd: float,
  *,
  live_interval_extra_budget_usd: float = 0.0,
) -> float:
  """Max deployable $ this hour (concurrent + cumulative) including win boost."""
  boost = live_win_cap_boost(settings, interval_realized_pnl_usd)
  extra = max(0.0, float(live_interval_extra_budget_usd))
  return float(max_cap) + boost + extra


def _clamp_profit_use_pct(pct: float) -> float:
  return max(0.0, min(100.0, float(pct)))


def _realized_adjustment(
  realized: float,
  *,
  use_accumulated_profit: bool,
  profit_use_pct: float,
) -> float:
  if not use_accumulated_profit:
    return realized
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
    # Paper bankroll should always accumulate P&L across hours/slots; the
    # "use_accumulated_profit" toggle is intended for live interval budgeting.
    paper = max(0.0, float(paper_bankroll_usd))
    cap = float(max_cap)
    if paper <= cap:
      return paper
    return cap + (paper - cap) * pct
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
  live_interval_extra_budget_usd: float = 0.0,
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
  if settings.mode == "paper":
    return max(0.0, min(deploy, float(max_cap)) - open_exposure_usd)

  if _live_auto_refill_enabled(settings):
    effective_max = live_effective_hour_cap(
      max_cap,
      settings,
      interval_realized_pnl_usd,
      live_interval_extra_budget_usd=live_interval_extra_budget_usd,
    )
    concurrent_room = max(0.0, min(deploy, effective_max) - open_exposure_usd)
    interval_room = max(0.0, effective_max - float(interval_total_entered_usd))
    return min(concurrent_room, interval_room)

  concurrent_room = max(0.0, min(deploy, float(max_cap)) - open_exposure_usd)
  if settings.use_accumulated_profit:
    return concurrent_room
  effective_cap = float(max_cap) + max(0.0, float(live_interval_extra_budget_usd))
  interval_room = max(0.0, effective_cap - float(interval_total_entered_usd))
  return min(concurrent_room, interval_room)


def config_max_spend_per_hour(cfg: dict | None) -> float | None:
  """Read hourly bot max concurrent exposure from asset-scoped config."""
  if not cfg:
    return None
  raw = ((cfg.get("hourly") or {}).get("bot") or {}).get("max_spend_per_hour_usd")
  if raw is None:
    return None
  return float(raw)


def sync_max_spend_from_config(store: Any, *, cfg: dict | None = None) -> None:
  """Clamp stored max spend down to config when config is lower (deploy safety)."""
  cap = config_max_spend_per_hour(cfg)
  if cap is None:
    return
  settings = store.get_settings()
  current = float(settings.max_spend_per_hour_usd)
  if current <= cap:
    return
  merged = settings.to_dict()
  merged["max_spend_per_hour_usd"] = cap
  store.save_settings(type(settings)(**merged), source="config_sync", cfg=cfg)
