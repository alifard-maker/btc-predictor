"""Actionable-bet and hour-quality labels for hourly primary picks."""

from __future__ import annotations

from typing import Any

from src.trading.contract_signals import is_actionable_buy


def assess_hourly_bet(
  *,
  signal: str | None,
  edge: float | None,
  regime_allow_trade: bool,
  regime_reasons: list[str] | None = None,
  expected_move_pct: float | None = None,
  min_edge: float = 0.05,
  min_expected_move_pct: float = 0.12,
  no_signal_detail: str = "No BUY YES/NO on primary pick",
) -> dict[str, Any]:
  """Return bold UI labels for whether this pick is an actionable bet."""
  reasons = list(regime_reasons or [])
  reasons_lc = " ".join(reasons).lower()
  compressed = "compressed" in reasons_lc or "compression" in reasons_lc

  edge_f = float(edge) if edge is not None else None
  move_f = float(expected_move_pct) if expected_move_pct is not None else None
  has_actionable_signal = is_actionable_buy(signal)
  edge_ok = edge_f is not None and abs(edge_f) >= min_edge
  move_ok = move_f is not None and abs(move_f) >= min_expected_move_pct
  move_strong = move_f is not None and abs(move_f) >= min_expected_move_pct * 1.5
  edge_strong = edge_f is not None and abs(edge_f) >= 0.10

  actionable = bool(regime_allow_trade and has_actionable_signal and edge_ok)

  if not regime_allow_trade or not move_ok or compressed:
    hour_quality = "WEAK"
  elif regime_allow_trade and move_strong and edge_strong and not compressed:
    hour_quality = "STRONG"
  else:
    hour_quality = "MODERATE"

  if actionable and hour_quality == "STRONG":
    actionable_headline = "STRONG ACTIONABLE BET"
    actionable_tone = "strong"
  elif actionable:
    actionable_headline = "ACTIONABLE BET"
    actionable_tone = "moderate"
  else:
    actionable_headline = "NOT STRONG AS AN ACTIONABLE BET"
    actionable_tone = "weak"

  detail_parts: list[str] = []
  if not has_actionable_signal:
    detail_parts.append(no_signal_detail)
  if not regime_allow_trade:
    detail_parts.append("Regime blocked")
  if edge_f is not None and not edge_ok:
    detail_parts.append(f"Edge {edge_f * 100:.1f}¢ below {min_edge * 100:.0f}¢ minimum")
  if move_f is not None and not move_ok:
    detail_parts.append(f"Expected move {move_f:.2f}% below {min_expected_move_pct:.2f}% floor")
  if compressed:
    detail_parts.append("Range compressed — chop")

  return {
    "actionable_bet": actionable,
    "actionable_headline": actionable_headline,
    "actionable_tone": actionable_tone,
    "hour_quality": hour_quality,
    "hour_quality_label": f"HOUR QUALITY FOR BETTING: {hour_quality}",
    "detail": " · ".join(detail_parts) if detail_parts else None,
  }


def _expected_move_pct_from(data: dict[str, Any] | None) -> float | None:
  if not data:
    return None
  raw = data.get("expected_move_pct")
  if raw is not None:
    return float(raw)
  cp = data.get("current_price") or data.get("reference_price")
  mu = data.get("terminal_mu") or data.get("blended_mu")
  if cp and mu:
    return (float(mu) - float(cp)) / float(cp) * 100
  return None


def assess_contract_bet(
  *,
  signal: str | None,
  edge: float | None,
  live: dict[str, Any],
  locked: dict[str, Any] | None = None,
  use_live_regime: bool = False,
  cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
  """Assess any hourly contract row (range/threshold) using shared regime context."""
  src = live if use_live_regime else (locked or live)
  regime = src.get("regime") or live.get("regime") or {}
  hcfg = (cfg or {}).get("hourly", {}).get("regime", {})
  move = _expected_move_pct_from(src) or _expected_move_pct_from(live)
  return assess_hourly_bet(
    signal=signal,
    edge=edge,
    regime_allow_trade=bool(regime.get("allow_trade", True)),
    regime_reasons=list(regime.get("reasons") or []),
    expected_move_pct=move,
    min_edge=float(hcfg.get("min_edge", 0.05)),
    min_expected_move_pct=float(hcfg.get("min_expected_move_pct", 0.12)),
    no_signal_detail="No BUY YES/NO on this contract",
  )


def assess_hourly_bet_from_row(row: dict[str, Any], cfg: dict[str, Any] | None = None) -> dict[str, Any]:
  """Recompute bet assessment from a logged hourly_predictions row."""
  hcfg = (cfg or {}).get("hourly", {}).get("regime", {})
  regime_reasons = [s for s in str(row.get("regime_notes") or "").split("; ") if s]
  return assess_hourly_bet(
    signal=row.get("primary_signal"),
    edge=row.get("primary_edge"),
    regime_allow_trade=not bool(row.get("regime_blocked")),
    regime_reasons=regime_reasons,
    expected_move_pct=row.get("expected_move_pct"),
    min_edge=float(hcfg.get("min_edge", 0.05)),
    min_expected_move_pct=float(hcfg.get("min_expected_move_pct", 0.12)),
  )


def assess_hourly_bet_from_late_call_row(row: dict[str, Any], cfg: dict[str, Any] | None = None) -> dict[str, Any]:
  """Recompute bet assessment from late_call_* columns on an hourly_predictions row."""
  hcfg = (cfg or {}).get("hourly", {}).get("regime", {})
  regime_reasons = [s for s in str(row.get("late_call_regime_notes") or "").split("; ") if s]
  return assess_hourly_bet(
    signal=row.get("late_call_primary_signal"),
    edge=row.get("late_call_primary_edge"),
    regime_allow_trade=not bool(row.get("late_call_regime_blocked")),
    regime_reasons=regime_reasons,
    expected_move_pct=row.get("late_call_expected_move_pct"),
    min_edge=float(hcfg.get("min_edge", 0.05)),
    min_expected_move_pct=float(hcfg.get("min_expected_move_pct", 0.12)),
  )
