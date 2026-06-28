"""Actionable-bet and slot-quality labels for 15-minute LONG/SHORT/NO TRADE."""

from __future__ import annotations

from typing import Any

from src.trading.edge import Signal


_ACTIONABLE = {Signal.LONG.value, Signal.SHORT.value}


def assess_slot15_bet(
  *,
  signal: str | None,
  model_signal: str | None = None,
  regime_allow_trade: bool = True,
  regime_reasons: list[str] | None = None,
  prob_up: float = 0.5,
  expected_move_pct: float | None = None,
  min_confidence: float = 0.57,
  min_expected_move_pct: float = 0.08,
) -> dict[str, Any]:
  """Return bold UI labels for whether this 15m slot is an actionable bet."""
  sig = str(signal or Signal.NO_TRADE.value)
  model_sig = str(model_signal or sig)
  reasons = list(regime_reasons or [])
  reasons_lc = " ".join(reasons).lower()
  compressed = "compressed" in reasons_lc or "compression" in reasons_lc

  has_actionable = sig in _ACTIONABLE
  edge_ok = (sig == Signal.LONG.value and prob_up >= min_confidence) or (
    sig == Signal.SHORT.value and prob_up <= 1.0 - min_confidence
  )
  actionable = bool(regime_allow_trade and has_actionable and edge_ok)

  move_f = float(expected_move_pct) if expected_move_pct is not None else None
  move_ok = move_f is not None and abs(move_f) >= min_expected_move_pct
  move_strong = move_f is not None and abs(move_f) >= min_expected_move_pct * 1.5
  prob_decisive = prob_up >= 0.62 or prob_up <= 0.38

  if not regime_allow_trade or not move_ok or compressed:
    slot_quality = "WEAK"
  elif regime_allow_trade and move_strong and prob_decisive and not compressed:
    slot_quality = "STRONG"
  else:
    slot_quality = "MODERATE"

  if actionable and slot_quality == "STRONG":
    actionable_headline = "STRONG ACTIONABLE BET"
    actionable_tone = "strong"
  elif actionable:
    actionable_headline = "ACTIONABLE BET"
    actionable_tone = "moderate"
  else:
    actionable_headline = "NOT STRONG AS AN ACTIONABLE BET"
    actionable_tone = "weak"

  detail_parts: list[str] = []
  if sig == Signal.NO_TRADE.value and model_sig in _ACTIONABLE:
    detail_parts.append(f"Model had {model_sig} — regime vetoed")
  elif not has_actionable:
    detail_parts.append("Signal is NO TRADE")
  if not regime_allow_trade and sig in _ACTIONABLE:
    detail_parts.append("Regime blocked")
  if has_actionable and not edge_ok:
    detail_parts.append(f"Prob {prob_up * 100:.1f}% below {min_confidence * 100:.0f}% confidence floor")
  if move_f is not None and not move_ok:
    detail_parts.append(f"Expected move {move_f:.3f}% below {min_expected_move_pct:.2f}% floor")
  if compressed:
    detail_parts.append("Range compressed — chop")

  return {
    "actionable_bet": actionable,
    "actionable_headline": actionable_headline,
    "actionable_tone": actionable_tone,
    "slot_quality": slot_quality,
    "slot_quality_label": f"SLOT QUALITY FOR BETTING: {slot_quality}",
    "detail": " · ".join(detail_parts) if detail_parts else None,
  }


def _regime_allow_trade(pred: Any) -> bool:
  model_sig = pred.model_signal or pred.signal.value
  sig = pred.signal.value
  if model_sig in _ACTIONABLE and sig == Signal.NO_TRADE.value:
    return False
  return True


def assess_slot15_from_prediction(pred: Any, cfg: dict[str, Any]) -> dict[str, Any]:
  rcfg = cfg.get("regime") or cfg.get("intra_slot", {}).get("regime") or {}
  min_move = float(
    rcfg.get(
      "min_expected_move_pct",
      cfg.get("intra_slot", {}).get("fee_buffer_pct", 0.08),
    )
  )
  min_conf = float(cfg.get("min_edge_confidence", 0.57))
  ref = pred.reference_price or pred.price
  expected_move_pct = (pred.expected_move / ref * 100) if ref else 0.0
  return assess_slot15_bet(
    signal=pred.signal.value,
    model_signal=pred.model_signal,
    regime_allow_trade=_regime_allow_trade(pred),
    regime_reasons=pred.regime_notes,
    prob_up=pred.prob_up,
    expected_move_pct=expected_move_pct,
    min_confidence=min_conf,
    min_expected_move_pct=min_move,
  )
