"""Build locked and hour-open hourly prediction views from logged DB rows."""

from __future__ import annotations

from typing import Any, Literal

from src.models.hourly_range_log import (
  RANGE_BE_PREFIX,
  RANGE_ML_PREFIX,
  parse_lean_bands,
  row_to_contract,
)
from src.trading.hourly_bet_assessment import assess_hourly_bet_from_late_call_row, assess_hourly_bet_from_row
from src.trading.hourly_position_alert import assess_late_call_position_alert_from_row, assess_locked_position_alert_from_row


def _zone_from_mu_sigma(mu: float | None, sigma: float | None) -> tuple[float | None, float | None]:
  if mu is None or sigma is None:
    return None, None
  return round(mu - sigma * 0.45, 2), round(mu + sigma * 0.45, 2)


def _lean_band_api(band: dict[str, Any]) -> dict[str, Any]:
  return {
    "ticker": band.get("ticker"),
    "contract_type": "range",
    "label": band.get("label"),
    "model_prob": band.get("model_prob"),
    "kalshi_mid": band.get("kalshi_mid"),
    "edge": band.get("edge"),
    "signal": band.get("signal"),
    "strike_type": "between",
    "floor_strike": band.get("floor"),
    "cap_strike": band.get("cap"),
  }


def snapshot_prediction_from_row(
  row: dict[str, Any],
  cfg: dict[str, Any] | None = None,
  *,
  kind: Literal["locked", "hour_open"] = "locked",
  index_label: str = "BRTI",
) -> dict[str, Any]:
  """Reconstruct API-shaped snapshot from hourly_predictions or hour-open row."""
  mu = row.get("terminal_mu") or row.get("blended_mu")
  sigma = row.get("terminal_sigma")
  mu_f = float(mu) if mu is not None else None
  sigma_f = float(sigma) if sigma is not None else None
  zone_lo = row.get("settlement_zone_low")
  zone_hi = row.get("settlement_zone_high")
  if zone_lo is None or zone_hi is None:
    zone_lo, zone_hi = _zone_from_mu_sigma(mu_f, sigma_f)
  else:
    zone_lo, zone_hi = float(zone_lo), float(zone_hi)

  pick = {
    "ticker": row.get("primary_ticker"),
    "contract_type": row.get("primary_type") or "threshold",
    "label": row.get("primary_label"),
    "model_prob": row.get("primary_model_prob"),
    "kalshi_mid": row.get("primary_kalshi_mid"),
    "edge": row.get("primary_edge"),
    "signal": row.get("primary_signal"),
    "strike_type": row.get("primary_strike_type"),
    "floor_strike": row.get("primary_floor"),
    "cap_strike": row.get("primary_cap"),
  }
  ml_threshold = None
  if row.get("most_likely_label"):
    ml_threshold = {
      "label": row.get("most_likely_label"),
      "model_prob": row.get("most_likely_prob"),
    }
  ml_range = row_to_contract(row, RANGE_ML_PREFIX)
  be_range = row_to_contract(row, RANGE_BE_PREFIX)
  lean_bands = [_lean_band_api(b) for b in parse_lean_bands(row)]

  if kind == "hour_open":
    zone_summary = (
      f"Hour-open {index_label} ${zone_lo:,.0f}–${zone_hi:,.0f} at settle"
      if zone_lo is not None and zone_hi is not None
      else ""
    )
    range_summary = (
      f"Hour-open stall band: {ml_range['label']} ({float(ml_range['model_prob']) * 100:.0f}% model)"
      if ml_range and ml_range.get("model_prob") is not None
      else "Range band odds at hour open"
    )
  else:
    zone_summary = (
      f"Locked {index_label} ${zone_lo:,.0f}–${zone_hi:,.0f} at settle"
      if zone_lo is not None and zone_hi is not None
      else ""
    )
    range_summary = (
      f"Locked stall band: {ml_range['label']} ({float(ml_range['model_prob']) * 100:.0f}% model)"
      if ml_range and ml_range.get("model_prob") is not None
      else "Range band odds at lock"
    )

  out = {
    "ok": True,
    "locked": kind == "locked",
    "hour_open": kind == "hour_open",
    "snapshot_kind": kind,
    "logged_at": row.get("logged_at"),
    "event_ticker": row.get("event_ticker"),
    "reference_price": row.get("reference_price"),
    "terminal_mu": mu_f,
    "terminal_sigma": sigma_f,
    "blended_mu": row.get("blended_mu"),
    "structure_mu": row.get("structure_mu"),
    "ml_mu": row.get("ml_mu"),
    "ml_prob_up": row.get("ml_prob_up"),
    "hours_to_settle": row.get("hours_to_settle"),
    "method": row.get("method"),
    "confidence": row.get("confidence"),
    "direction": row.get("direction"),
    "prob_15m_avg": row.get("prob_15m_avg"),
    "primary_pick": pick,
    "regime": {
      "allow_trade": not bool(row.get("regime_blocked")),
      "reasons": [s for s in str(row.get("regime_notes") or "").split("; ") if s],
    },
    "most_likely": {
      "settlement_zone_low": zone_lo,
      "settlement_zone_high": zone_hi,
      "summary": zone_summary,
      "threshold": ml_threshold,
      "range": ml_range,
    },
    "strategy_range": {
      "summary": range_summary,
      "most_likely": ml_range,
      "best_edge": be_range,
      "lean_bands": lean_bands,
    },
    "settle_brti": row.get("settle_brti"),
    "event": {
      "event_ticker": row.get("event_ticker"),
      "series_ticker": row.get("series_ticker"),
      "frequency": row.get("frequency"),
      "title": row.get("title"),
      "close_time": row.get("settle_time"),
    },
    "expected_move_pct": row.get("expected_move_pct"),
    "bet_assessment": assess_hourly_bet_from_row(row, cfg),
  }
  if kind == "locked":
    out["position_alert"] = assess_locked_position_alert_from_row(row, cfg)
  return out


def locked_prediction_from_row(row: dict[str, Any], cfg: dict[str, Any] | None = None) -> dict[str, Any]:
  """Reconstruct API-shaped locked prediction from hourly_predictions row."""
  return snapshot_prediction_from_row(row, cfg, kind="locked")


def hour_open_prediction_from_row(
  row: dict[str, Any],
  cfg: dict[str, Any] | None = None,
  *,
  index_label: str = "BRTI",
) -> dict[str, Any]:
  """Reconstruct API-shaped hour-open snapshot (not used for calibration scoring)."""
  return snapshot_prediction_from_row(row, cfg, kind="hour_open", index_label=index_label)


def late_call_prediction_from_row(row: dict[str, Any], cfg: dict[str, Any] | None = None) -> dict[str, Any] | None:
  """Reconstruct API-shaped late-call prediction from late_call_* columns.

  Trading guidance only — not mixed into :05 calibration / Brier scoring.
  """
  if not row.get("late_call_logged_at"):
    return None

  pick = {
    "ticker": row.get("late_call_primary_ticker"),
    "contract_type": row.get("late_call_primary_type") or "threshold",
    "label": row.get("late_call_primary_label"),
    "model_prob": row.get("late_call_primary_model_prob"),
    "kalshi_mid": row.get("late_call_primary_kalshi_mid"),
    "edge": row.get("late_call_primary_edge"),
    "signal": row.get("late_call_primary_signal"),
    "strike_type": row.get("late_call_primary_strike_type"),
    "floor_strike": row.get("late_call_primary_floor"),
    "cap_strike": row.get("late_call_primary_cap"),
  }

  return {
    "ok": True,
    "late_call": True,
    "logged_at": row.get("late_call_logged_at"),
    "event_ticker": row.get("event_ticker"),
    "reference_price": row.get("late_call_reference_price"),
    "method": row.get("late_call_method"),
    "confidence": row.get("late_call_confidence"),
    "direction": row.get("late_call_direction"),
    "prob_15m_avg": row.get("late_call_prob_15m_avg"),
    "ml_prob_up": row.get("late_call_ml_prob_up"),
    "expected_move_pct": row.get("late_call_expected_move_pct"),
    "primary_pick": pick,
    "regime": {
      "allow_trade": not bool(row.get("late_call_regime_blocked")),
      "reasons": [s for s in str(row.get("late_call_regime_notes") or "").split("; ") if s],
    },
    "event": {
      "event_ticker": row.get("event_ticker"),
      "series_ticker": row.get("series_ticker"),
      "frequency": row.get("frequency"),
      "title": row.get("title"),
      "close_time": row.get("settle_time"),
    },
    "bet_assessment": assess_hourly_bet_from_late_call_row(row, cfg),
    "position_alert": assess_late_call_position_alert_from_row(row, cfg),
  }
