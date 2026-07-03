"""Structured exit context for hourly bot trades (index, contract, thesis at exit)."""

from __future__ import annotations

from typing import Any

from src.trading.bot_live_exit import quick_exit_applies
from src.trading.bot_profit_exit import hourly_mark_cut_allowed, position_hold_seconds
from src.trading.hourly_position_alert import (
  _signal_favors_held_side,
  _spot_favors_held_side,
)


def _contract_label(pick: dict[str, Any] | None, pos: dict[str, Any] | None) -> str | None:
  src = pick or pos or {}
  ctype = src.get("contract_type")
  floor = src.get("floor_strike")
  cap = src.get("cap_strike")
  strike_type = src.get("strike_type")
  if ctype == "range" or strike_type == "between":
    if floor is not None and cap is not None:
      return f"${float(floor):,.2f}–${float(cap):,.2f}"
  if strike_type == "greater" and floor is not None:
    return f"≥ ${float(floor):,.2f}"
  if strike_type == "less" and cap is not None:
    return f"< ${float(cap):,.2f}"
  label = src.get("label")
  return str(label) if label else None


def _spot_status(spot_favors: bool | None) -> str:
  if spot_favors is True:
    return "supports"
  if spot_favors is False:
    return "against"
  return "unknown"


def _signal_status(sig_favors: bool | None) -> str:
  if sig_favors is True:
    return "supports"
  if sig_favors is False:
    return "against"
  return "neutral"


def build_hourly_exit_context(
  *,
  pos: dict[str, Any],
  pick: dict[str, Any] | None,
  tab: dict[str, Any],
  live_price: float | None,
  unrealized_pnl_usd: float | None,
  exit_reason: str,
  position_alert: dict[str, Any] | None = None,
  standard_hourly_alert: str | None = None,
  bot_kind: str = "hourly",
  hours_to_settle: float | None = None,
  cfg: dict[str, Any] | None = None,
  adaptive_mode: str | None = None,
  hour_momentum_state: str | None = None,
) -> dict[str, Any]:
  """Snapshot index, contract, and thesis state at exit for post-trade review."""
  live = tab.get("live") or tab
  index_id = str(live.get("index_id") or live.get("settlement_reference") or "BRTI")
  side = str(pos.get("side") or "yes")

  spot_favors: bool | None = None
  if live_price is not None and pick:
    try:
      spot_favors = _spot_favors_held_side(
        side=side,
        live_price=float(live_price),
        pick=pick,
      )
    except (TypeError, ValueError):
      spot_favors = None

  live_signal = pick.get("signal") if pick else None
  sig_favors = _signal_favors_held_side(live_signal, side) if pick else None
  regime = live.get("regime") or {}

  ctx: dict[str, Any] = {
    "bot_kind": bot_kind,
    "exit_reason": exit_reason,
    "index_id": index_id,
    "index_live": float(live_price) if live_price is not None else None,
    "entry_reference_price": pos.get("reference_price"),
    "hours_to_settle": hours_to_settle,
    "side": side,
    "entry_signal": pos.get("signal"),
    "live_signal": live_signal,
    "entry_edge": pos.get("entry_edge"),
    "live_edge": pick.get("edge") if pick else None,
    "contract_type": (pick or pos).get("contract_type"),
    "strike_type": (pick or pos).get("strike_type"),
    "floor_strike": (pick or pos).get("floor_strike"),
    "cap_strike": (pick or pos).get("cap_strike"),
    "contract_label": _contract_label(pick, pos),
    "market_ticker": pos.get("market_ticker"),
    "unrealized_pnl_usd": unrealized_pnl_usd,
    "spot_favors_held_side": spot_favors,
    "signal_favors_held_side": sig_favors,
    "thesis_broken": hourly_mark_cut_allowed(pos, pick, live_price),
    "regime_allow_trade": bool(regime.get("allow_trade", True)),
    "regime_reasons": list(regime.get("reasons") or []),
  }

  if position_alert:
    ctx["position_alert"] = position_alert.get("alert")
    ctx["position_alert_detail"] = position_alert.get("detail")
  if standard_hourly_alert:
    ctx["standard_hourly_alert"] = standard_hourly_alert

  hold = position_hold_seconds(pos)
  ctx["hold_seconds"] = hold
  ctx["quick_exit_applied"] = quick_exit_applies(
    cfg,
    kind="hourly",
    adaptive_mode=adaptive_mode,
    hour_momentum_state=hour_momentum_state,
  )
  if pos.get("contract_mismatch"):
    ctx["contract_mismatch"] = pos["contract_mismatch"]

  return ctx


def format_hourly_exit_context_detail(ctx: dict[str, Any]) -> str:
  """Compact human-readable vet line appended to trade detail."""
  parts: list[str] = []
  index_id = ctx.get("index_id") or "INDEX"
  index_live = ctx.get("index_live")
  if index_live is not None:
    parts.append(f"{index_id} ${float(index_live):,.2f}")

  contract = ctx.get("contract_label")
  if contract:
    parts.append(f"contract {contract}")

  entry_ref = ctx.get("entry_reference_price")
  if entry_ref is not None:
    parts.append(f"entry ref ${float(entry_ref):,.2f}")

  parts.append(f"spot { _spot_status(ctx.get('spot_favors_held_side')) }")
  parts.append(f"signal { _signal_status(ctx.get('signal_favors_held_side')) }")

  live_signal = ctx.get("live_signal")
  if live_signal:
    parts.append(f"live {live_signal}")

  alert = ctx.get("position_alert")
  if alert:
    parts.append(f"alert {alert}")

  std = ctx.get("standard_hourly_alert")
  if std and std != alert:
    parts.append(f"std hourly {std}")

  hours = ctx.get("hours_to_settle")
  if hours is not None:
    parts.append(f"{float(hours):.2f}h to settle")

  return "Vet: " + " · ".join(parts)
