"""Helpers for logging range-band contract fields on hourly rows."""

from __future__ import annotations

from typing import Any

RANGE_ML_PREFIX = "range_ml"
RANGE_BE_PREFIX = "range_be"

RANGE_BAND_LOG_FIELDS: tuple[str, ...] = (
  "range_ml_ticker",
  "range_ml_label",
  "range_ml_prob",
  "range_ml_signal",
  "range_ml_edge",
  "range_ml_floor",
  "range_ml_cap",
  "range_ml_kalshi_mid",
  "range_be_ticker",
  "range_be_label",
  "range_be_prob",
  "range_be_signal",
  "range_be_edge",
  "range_be_floor",
  "range_be_cap",
  "range_be_kalshi_mid",
)


def contract_to_row_prefix(contract: dict[str, Any] | None, prefix: str) -> dict[str, Any]:
  keys = ("ticker", "label", "prob", "signal", "edge", "floor", "cap", "kalshi_mid")
  if not contract:
    return {f"{prefix}_{k}": None for k in keys}
  return {
    f"{prefix}_ticker": contract.get("ticker"),
    f"{prefix}_label": contract.get("label"),
    f"{prefix}_prob": contract.get("model_prob"),
    f"{prefix}_signal": contract.get("signal"),
    f"{prefix}_edge": contract.get("edge"),
    f"{prefix}_floor": contract.get("floor_strike"),
    f"{prefix}_cap": contract.get("cap_strike"),
    f"{prefix}_kalshi_mid": contract.get("kalshi_mid"),
  }


def row_to_contract(row: dict[str, Any], prefix: str) -> dict[str, Any] | None:
  label = row.get(f"{prefix}_label")
  if not label:
    return None
  return {
    "ticker": row.get(f"{prefix}_ticker"),
    "contract_type": "range",
    "label": label,
    "model_prob": row.get(f"{prefix}_prob"),
    "kalshi_mid": row.get(f"{prefix}_kalshi_mid"),
    "edge": row.get(f"{prefix}_edge"),
    "signal": row.get(f"{prefix}_signal"),
    "strike_type": "between",
    "floor_strike": row.get(f"{prefix}_floor"),
    "cap_strike": row.get(f"{prefix}_cap"),
  }


def range_band_migrations() -> tuple[tuple[str, str], ...]:
  out: list[tuple[str, str]] = [
    ("settlement_zone_low", "REAL"),
    ("settlement_zone_high", "REAL"),
  ]
  for f in RANGE_BAND_LOG_FIELDS:
    typ = "REAL" if f.endswith(("_prob", "_edge", "_floor", "_cap", "_kalshi_mid")) else "TEXT"
    out.append((f, typ))
  return tuple(out)


def range_band_migrations_pg() -> tuple[tuple[str, str], ...]:
  return tuple(
    (col, "DOUBLE PRECISION" if typ == "REAL" else "TEXT")
    for col, typ in range_band_migrations()
  )
