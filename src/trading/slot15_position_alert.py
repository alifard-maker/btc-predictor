"""Per-leg position alerts for the 15-minute auto-bet bot (contract mark P&L, not slot BRTI only)."""

from __future__ import annotations

from typing import Any

from src.trading.bot_profit_exit import (
  AdaptiveExitContext,
  ProfitExitSettings,
  evaluate_slot15_contract_exits,
  position_hold_seconds,
)

AlertKind = str
ToneKind = str


def _result(
  alert: AlertKind,
  tone: ToneKind,
  detail: str,
  *,
  slot_monitor_alert: str | None = None,
  slot_monitor_detail: str | None = None,
  mark_vs_entry_cents: int | None = None,
) -> dict[str, Any]:
  out: dict[str, Any] = {
    "alert": alert,
    "alert_tone": tone,
    "headline": alert,
    "detail": detail,
  }
  if slot_monitor_alert:
    out["slot_monitor_alert"] = slot_monitor_alert
  if slot_monitor_detail:
    out["slot_monitor_detail"] = slot_monitor_detail
  if mark_vs_entry_cents is not None:
    out["mark_vs_entry_cents"] = mark_vs_entry_cents
  return out


def _slot_monitor_context(monitor: dict[str, Any] | None) -> tuple[str, str]:
  mon = monitor or {}
  action = str(mon.get("action") or "HOLD")
  if action == "CUT LOSS":
    action = "CUT LOSSES"
  message = str(mon.get("message") or mon.get("reassess_summary") or "")
  return action, message


def _mark_vs_entry_cents(pos: dict[str, Any], mark_cents: int | None) -> int | None:
  if mark_cents is None:
    return None
  entry_c = int(pos.get("entry_price_cents") or 0)
  if entry_c <= 0:
    return None
  return int(mark_cents) - entry_c


def assess_slot15_leg_position_alert(
  *,
  pos: dict[str, Any],
  mark_cents: int | None,
  unrealized_pnl_usd: float | None,
  monitor: dict[str, Any] | None,
  cfg: dict[str, Any] | None = None,
  settings: ProfitExitSettings | None = None,
  peaks: dict[str, float] | None = None,
  exit_ctx: AdaptiveExitContext | None = None,
) -> dict[str, Any]:
  """Contract-aware alert for one open leg; slot monitor shown as secondary context."""
  slot_alert, slot_detail = _slot_monitor_context(monitor)
  mark_delta = _mark_vs_entry_cents(pos, mark_cents)
  peaks = peaks or {}
  hold_seconds = position_hold_seconds(pos)
  ctx = exit_ctx or AdaptiveExitContext(period_seconds=900.0)

  def _with_slot(alert: str, tone: str, detail: str) -> dict[str, Any]:
    return _result(
      alert,
      tone,
      detail,
      slot_monitor_alert=slot_alert,
      slot_monitor_detail=slot_detail,
      mark_vs_entry_cents=mark_delta,
    )

  reason, detail = evaluate_slot15_contract_exits(
    pos=pos,
    mark_cents=mark_cents,
    unrealized_usd=unrealized_pnl_usd,
    monitor=monitor or {},
    peaks=peaks,
    hold_seconds=hold_seconds,
    settings=settings,
    exit_ctx=ctx,
    cfg=cfg,
    include_monitor_fallback=False,
  )
  if reason in ("LEG STOP", "CHEAP LEG CUT LOSS"):
    return _with_slot("CUT LOSSES", "danger", detail)
  if reason in (
    "LEG TAKE PROFIT",
    "LEG TRAIL",
    "REASSESS NEUTRAL TP",
    "PROFIT TARGET",
    "PROFIT TRAIL",
  ):
    return _with_slot("TAKE PROFIT", "success", detail)

  if slot_alert == "TAKE PROFIT":
    return _with_slot(
      "TAKE PROFIT",
      "success",
      f"Slot BRTI monitor: {slot_detail or 'take profit signal'}",
    )
  if slot_alert == "CUT LOSSES":
    leg_note = ""
    if mark_delta is not None and unrealized_pnl_usd is not None:
      leg_note = f" Leg mark {mark_delta:+d}¢ ({unrealized_pnl_usd:+.2f} unrealized)."
    return _with_slot(
      "CUT LOSSES",
      "danger",
      f"Slot BRTI monitor: {slot_detail or 'cut loss signal'}.{leg_note}",
    )

  leg_parts: list[str] = []
  if mark_delta is not None:
    leg_parts.append(f"Mark {mark_delta:+d}¢ vs entry")
  if unrealized_pnl_usd is not None:
    leg_parts.append(f"{unrealized_pnl_usd:+.2f} unrealized")
  leg_line = " · ".join(leg_parts) if leg_parts else "Monitoring contract mark"
  hold_detail = f"{leg_line}. Slot (BRTI): {slot_alert}"
  if slot_detail:
    hold_detail += f" — {slot_detail}"
  return _with_slot("HOLD", "neutral", hold_detail)
