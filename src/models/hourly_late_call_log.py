"""Helpers for logging late-call (:45 ET) hourly prediction fields.

Late-call snapshots are trading guidance only — they are NOT mixed into
:05 calibration / Brier scoring (see HourlyCalibrationTracker.summary).
"""

from __future__ import annotations

from typing import Any

LATE_CALL_PREFIX = "late_call"

LATE_CALL_LOG_FIELDS: tuple[str, ...] = (
  "late_call_logged_at",
  "late_call_reference_price",
  "late_call_primary_ticker",
  "late_call_primary_type",
  "late_call_primary_label",
  "late_call_primary_strike_type",
  "late_call_primary_floor",
  "late_call_primary_cap",
  "late_call_primary_model_prob",
  "late_call_primary_kalshi_mid",
  "late_call_primary_edge",
  "late_call_primary_signal",
  "late_call_confidence",
  "late_call_expected_move_pct",
  "late_call_direction",
  "late_call_method",
  "late_call_regime_blocked",
  "late_call_regime_notes",
  "late_call_prob_15m_avg",
  "late_call_ml_prob_up",
)


def late_call_migrations() -> list[tuple[str, str]]:
  return [
    ("late_call_logged_at", "TEXT"),
    ("late_call_reference_price", "REAL"),
    ("late_call_primary_ticker", "TEXT"),
    ("late_call_primary_type", "TEXT"),
    ("late_call_primary_label", "TEXT"),
    ("late_call_primary_strike_type", "TEXT"),
    ("late_call_primary_floor", "REAL"),
    ("late_call_primary_cap", "REAL"),
    ("late_call_primary_model_prob", "REAL"),
    ("late_call_primary_kalshi_mid", "REAL"),
    ("late_call_primary_edge", "REAL"),
    ("late_call_primary_signal", "TEXT"),
    ("late_call_confidence", "REAL"),
    ("late_call_expected_move_pct", "REAL"),
    ("late_call_direction", "TEXT"),
    ("late_call_method", "TEXT"),
    ("late_call_regime_blocked", "INTEGER DEFAULT 0"),
    ("late_call_regime_notes", "TEXT"),
    ("late_call_prob_15m_avg", "REAL"),
    ("late_call_ml_prob_up", "REAL"),
  ]


def late_call_migrations_pg() -> list[tuple[str, str]]:
  return [
    (col, typ.replace("INTEGER DEFAULT 0", "INTEGER DEFAULT 0"))
    for col, typ in late_call_migrations()
  ]


def prediction_to_late_call_row(pred: dict[str, Any], *, logged_at: str) -> dict[str, Any]:
  """Map a live hourly prediction dict to late_call_* DB columns."""
  ev = pred.get("event") or {}
  pick = pred.get("primary_pick") or {}
  regime = pred.get("regime") or {}
  cp = pred.get("current_price")
  mu = pred.get("blended_mu")
  expected_move = pred.get("expected_move_pct")
  if expected_move is None and cp and mu:
    expected_move = (float(mu) - float(cp)) / float(cp) * 100
  return {
    "event_ticker": ev.get("event_ticker", ""),
    "late_call_logged_at": logged_at,
    "late_call_reference_price": cp,
    "late_call_primary_ticker": pick.get("ticker"),
    "late_call_primary_type": pick.get("contract_type", "threshold"),
    "late_call_primary_label": pick.get("label"),
    "late_call_primary_strike_type": pick.get("strike_type"),
    "late_call_primary_floor": pick.get("floor_strike"),
    "late_call_primary_cap": pick.get("cap_strike"),
    "late_call_primary_model_prob": pick.get("model_prob"),
    "late_call_primary_kalshi_mid": pick.get("kalshi_mid"),
    "late_call_primary_edge": pick.get("edge"),
    "late_call_primary_signal": pick.get("signal"),
    "late_call_confidence": pred.get("confidence"),
    "late_call_expected_move_pct": expected_move,
    "late_call_direction": pred.get("direction"),
    "late_call_method": pred.get("method"),
    "late_call_regime_blocked": 0 if regime.get("allow_trade", True) else 1,
    "late_call_regime_notes": "; ".join(regime.get("reasons") or []),
    "late_call_prob_15m_avg": pred.get("prob_15m_avg"),
    "late_call_ml_prob_up": pred.get("ml_prob_up"),
  }


def has_late_call(row: dict[str, Any] | None) -> bool:
  return bool(row and row.get("late_call_logged_at"))
