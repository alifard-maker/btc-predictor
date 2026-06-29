"""Structured exit context for 15m bot trades (BRTI/ERTI, slot monitor, reassess at exit)."""

from __future__ import annotations

from typing import Any


def _index_label(tab: dict[str, Any], asset: str) -> str:
  mon = tab.get("monitor") or {}
  for key in ("index_id", "index_label", "settlement_reference"):
    val = tab.get(key) or mon.get(key)
    if val:
      return str(val)
  return "ERTI" if asset.lower() == "eth" else "BRTI"


def _bet_side(pos: dict[str, Any], monitor: dict[str, Any]) -> str | None:
  bs = str(monitor.get("bet_side") or "").upper()
  if bs in ("UP", "DOWN"):
    return bs
  sig = str(pos.get("signal") or monitor.get("signal_at_open") or "").upper()
  if sig == "LONG":
    return "UP"
  if sig == "SHORT":
    return "DOWN"
  side = str(pos.get("side") or "").lower()
  if side == "yes":
    return "UP"
  if side == "no":
    return "DOWN"
  return None


def _reassess_supports(bet_side: str | None, prob_up: float | None) -> bool | None:
  if bet_side not in ("UP", "DOWN") or prob_up is None:
    return None
  if bet_side == "UP":
    return float(prob_up) >= 0.55
  return float(prob_up) <= 0.45


def _reassess_against(bet_side: str | None, prob_up: float | None) -> bool | None:
  if bet_side not in ("UP", "DOWN") or prob_up is None:
    return None
  if bet_side == "UP":
    return float(prob_up) <= 0.45
  return float(prob_up) >= 0.55


def _reassess_status(supports: bool | None, against: bool | None) -> str:
  if supports is True:
    return "supports"
  if against is True:
    return "against"
  return "neutral"


def build_slot15_exit_context(
  *,
  pos: dict[str, Any],
  tab: dict[str, Any],
  unrealized_pnl_usd: float | None,
  exit_reason: str,
  asset: str = "btc",
  leg_position_alert: dict[str, Any] | None = None,
) -> dict[str, Any]:
  """Snapshot index, slot monitor, and reassess state at exit for post-trade review."""
  monitor = tab.get("monitor") or {}
  index_id = _index_label(tab, asset)
  ref = monitor.get("reference_price")
  if ref is None:
    ref = pos.get("reference_price")
  current = monitor.get("current_price")
  bet = _bet_side(pos, monitor)
  prob_up = monitor.get("reassessed_prob_up")
  if prob_up is not None:
    try:
      prob_up = float(prob_up)
    except (TypeError, ValueError):
      prob_up = None

  supports = _reassess_supports(bet, prob_up)
  against = _reassess_against(bet, prob_up)
  brti_delta: float | None = None
  if ref is not None and current is not None:
    try:
      brti_delta = float(current) - float(ref)
    except (TypeError, ValueError):
      brti_delta = None

  slot_action = str(monitor.get("action") or "HOLD")
  if slot_action == "CUT LOSS":
    slot_action = "CUT LOSSES"

  ctx: dict[str, Any] = {
    "bot_kind": "slot15",
    "asset": asset.lower(),
    "exit_reason": exit_reason,
    "index_id": index_id,
    "index_live": float(current) if current is not None else None,
    "entry_reference_price": float(ref) if ref is not None else pos.get("reference_price"),
    "brti_move_usd": brti_delta,
    "seconds_remaining": monitor.get("seconds_remaining"),
    "elapsed_pct": monitor.get("elapsed_pct"),
    "side": pos.get("side"),
    "bet_side": bet,
    "entry_signal": pos.get("signal"),
    "signal_at_open": monitor.get("signal_at_open") or pos.get("signal"),
    "slot_monitor_action": slot_action,
    "slot_monitor_message": monitor.get("message") or monitor.get("reassess_summary"),
    "reassessed_prob_up": prob_up,
    "reassessed_close_side": monitor.get("reassessed_close_side"),
    "reassess_summary": monitor.get("reassess_summary"),
    "reassess_supports_bet": supports,
    "reassess_against_bet": against,
    "unrealized_pnl_usd": unrealized_pnl_usd,
    "slot_unrealized_pct": monitor.get("unrealized_pct"),
    "market_ticker": pos.get("market_ticker"),
    "slot_label": tab.get("slot_label") or monitor.get("slot_label"),
  }

  if leg_position_alert:
    ctx["leg_position_alert"] = leg_position_alert.get("alert")
    ctx["leg_position_alert_detail"] = leg_position_alert.get("detail")
    ctx["slot_monitor_alert"] = leg_position_alert.get("slot_monitor_alert")
    ctx["slot_monitor_detail"] = leg_position_alert.get("slot_monitor_detail")

  return ctx


def format_slot15_exit_context_detail(ctx: dict[str, Any]) -> str:
  """Compact human-readable vet line appended to trade detail."""
  parts: list[str] = []
  index_id = ctx.get("index_id") or "INDEX"
  index_live = ctx.get("index_live")
  if index_live is not None:
    parts.append(f"{index_id} ${float(index_live):,.2f}")

  entry_ref = ctx.get("entry_reference_price")
  if entry_ref is not None:
    parts.append(f"ref ${float(entry_ref):,.2f}")

  delta = ctx.get("brti_move_usd")
  if delta is not None:
    parts.append(f"Δ${float(delta):+,.0f}")

  bet = ctx.get("bet_side")
  if bet:
    parts.append(f"bet {bet}")

  prob = ctx.get("reassessed_prob_up")
  if prob is not None:
    parts.append(f"reassess {float(prob) * 100:.0f}% UP")
    parts.append(f"reassess {_reassess_status(ctx.get('reassess_supports_bet'), ctx.get('reassess_against_bet'))}")

  slot_action = ctx.get("slot_monitor_action")
  if slot_action:
    parts.append(f"slot {slot_action}")

  leg_alert = ctx.get("leg_position_alert")
  if leg_alert and leg_alert != slot_action:
    parts.append(f"leg {leg_alert}")

  secs = ctx.get("seconds_remaining")
  if secs is not None:
    parts.append(f"{int(secs)}s left")

  return "Vet: " + " · ".join(parts)
