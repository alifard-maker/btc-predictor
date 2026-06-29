"""Per-leg alerts for hourly trial bots (15m-style contract exits vs standard hourly view)."""

from __future__ import annotations

from typing import Any

from src.trading.bot_profit_exit import (
  AdaptiveExitContext,
  ProfitExitSettings,
  effective_hourly_trial_settings,
  evaluate_slot15_contract_exits,
  position_hold_seconds,
)
from src.trading.hourly_position_alert import assess_held_hourly_position_alert


def _mark_vs_entry_cents(pos: dict[str, Any], mark_cents: int | None) -> int | None:
  if mark_cents is None:
    return None
  entry_c = int(pos.get("entry_price_cents") or 0)
  if entry_c <= 0:
    return None
  return int(mark_cents) - entry_c


def assess_hourly_trial_leg_position_alert(
  *,
  pos: dict[str, Any],
  pick: dict[str, Any],
  mark_cents: int | None,
  unrealized_pnl_usd: float | None,
  live_price: float | None,
  regime_allow_trade: bool,
  regime_reasons: list[str],
  cfg: dict[str, Any] | None = None,
  settings: ProfitExitSettings | None = None,
  peaks: dict[str, float] | None = None,
  exit_ctx: AdaptiveExitContext | None = None,
  index_label: str = "BRTI",
) -> dict[str, Any]:
  """Contract-first leg alert; standard hourly signal shown as comparison context."""
  standard = assess_held_hourly_position_alert(
    pos=pos,
    pick=pick,
    live_price=live_price,
    regime_allow_trade=regime_allow_trade,
    regime_reasons=regime_reasons,
    unrealized_pnl_usd=unrealized_pnl_usd,
    cfg=cfg,
  )
  std_alert = str(standard.get("alert") or "HOLD")
  std_detail = str(standard.get("detail") or "")
  mark_delta = _mark_vs_entry_cents(pos, mark_cents)
  peaks = peaks or {}
  hold_seconds = position_hold_seconds(pos)
  ctx = exit_ctx or AdaptiveExitContext(period_seconds=3600.0)
  trial_settings = effective_hourly_trial_settings(settings, cfg) if settings else settings

  def _with_std(alert: str, tone: str, detail: str) -> dict[str, Any]:
    out = {
      "alert": alert,
      "alert_tone": tone,
      "headline": alert,
      "detail": detail,
      "standard_hourly_alert": std_alert,
      "standard_hourly_detail": std_detail,
    }
    if mark_delta is not None:
      out["mark_vs_entry_cents"] = mark_delta
    return out

  reason, detail = evaluate_slot15_contract_exits(
    pos=pos,
    mark_cents=mark_cents,
    unrealized_usd=unrealized_pnl_usd,
    monitor={},
    peaks=peaks,
    hold_seconds=hold_seconds,
    settings=trial_settings,
    exit_ctx=ctx,
    cfg=cfg,
    include_monitor_fallback=False,
    bot_kind="hourly_trial",
    pick=pick,
    live_price=live_price,
    standard_hourly_alert=std_alert,
  )
  if reason in ("LEG STOP", "CHEAP LEG CUT LOSS"):
    return _with_std("CUT LOSSES", "danger", detail)
  if reason in (
    "LEG TAKE PROFIT",
    "LEG TRAIL",
    "REASSESS NEUTRAL TP",
    "PROFIT TARGET",
    "PROFIT TRAIL",
  ):
    return _with_std("TAKE PROFIT", "success", detail)

  leg_parts: list[str] = []
  if mark_delta is not None:
    leg_parts.append(f"Mark {mark_delta:+d}¢ vs entry")
  if unrealized_pnl_usd is not None:
    leg_parts.append(f"{unrealized_pnl_usd:+.2f} unrealized")
  leg_line = " · ".join(leg_parts) if leg_parts else "Monitoring contract mark"
  hold_detail = f"{leg_line}. Standard hourly: {std_alert}"
  if std_detail:
    hold_detail += f" — {std_detail}"
  return _with_std("HOLD", "neutral", hold_detail)
