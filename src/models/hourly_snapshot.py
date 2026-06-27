"""Build locked hourly prediction views from logged DB rows."""

from __future__ import annotations

from typing import Any

from src.models.hourly_range_log import (
  RANGE_BE_PREFIX,
  RANGE_ML_PREFIX,
  parse_lean_bands,
  row_to_contract,
)


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


def locked_prediction_from_row(row: dict[str, Any]) -> dict[str, Any]:
  """Reconstruct API-shaped locked prediction from hourly_predictions row."""
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

  return {
    "ok": True,
    "locked": True,
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
      "summary": (
        f"Locked BRTI ${zone_lo:,.0f}–${zone_hi:,.0f} at settle"
        if zone_lo is not None and zone_hi is not None
        else ""
      ),
      "threshold": ml_threshold,
      "range": ml_range,
    },
    "strategy_range": {
      "summary": (
        f"Locked stall band: {ml_range['label']} ({float(ml_range['model_prob']) * 100:.0f}% model)"
        if ml_range and ml_range.get("model_prob") is not None
        else "Range band odds at lock"
      ),
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
  }
