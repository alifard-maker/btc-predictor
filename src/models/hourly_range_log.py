"""Helpers for logging range-band contract fields on hourly rows."""

from __future__ import annotations

import json
from typing import Any

from src.trading.contract_signals import is_actionable_buy, signal_correct_for_outcome

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
  "range_lean_bands",
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


def lean_bands_from_contracts(contracts: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
  """All range bands with BUY YES/NO at lock (near-forecast slice)."""
  out: list[dict[str, Any]] = []
  for c in contracts or []:
    sig = str(c.get("signal") or "")
    if not is_actionable_buy(sig):
      continue
    floor = c.get("floor_strike")
    cap = c.get("cap_strike")
    if floor is None or cap is None:
      continue
    out.append(
      {
        "ticker": c.get("ticker"),
        "label": c.get("label"),
        "model_prob": c.get("model_prob"),
        "signal": sig,
        "edge": c.get("edge"),
        "floor": floor,
        "cap": cap,
        "kalshi_mid": c.get("kalshi_mid"),
      }
    )
  return out


def serialize_lean_bands(bands: list[dict[str, Any]]) -> str | None:
  if not bands:
    return None
  return json.dumps(bands)


def parse_lean_bands(row: dict[str, Any]) -> list[dict[str, Any]]:
  raw = row.get("range_lean_bands")
  if not raw:
    return []
  if isinstance(raw, list):
    return raw
  try:
    data = json.loads(raw)
    return data if isinstance(data, list) else []
  except (TypeError, json.JSONDecodeError):
    return []


def band_outcome_from_band(settle_brti: float, band: dict[str, Any]) -> int | None:
  lo = band.get("floor")
  hi = band.get("cap")
  if lo is None or hi is None:
    return None
  try:
    s, lo_f, hi_f = float(settle_brti), float(lo), float(hi)
  except (TypeError, ValueError):
    return None
  return 1 if lo_f <= s <= hi_f else 0


def band_outcome_from_row(row: dict[str, Any], prefix: str) -> int | None:
  """1 if settle BRTI landed inside the band, 0 if not, None if unknown."""
  settle = row.get("settle_brti")
  lo = row.get(f"{prefix}_floor")
  hi = row.get(f"{prefix}_cap")
  if settle is None or lo is None or hi is None:
    return None
  try:
    s, lo_f, hi_f = float(settle), float(lo), float(hi)
  except (TypeError, ValueError):
    return None
  return 1 if lo_f <= s <= hi_f else 0


def range_band_migrations() -> tuple[tuple[str, str], ...]:
  out: list[tuple[str, str]] = [
    ("settlement_zone_low", "REAL"),
    ("settlement_zone_high", "REAL"),
  ]
  for f in RANGE_BAND_LOG_FIELDS:
    if f == "range_lean_bands":
      typ = "TEXT"
    elif f.endswith(("_prob", "_edge", "_floor", "_cap", "_kalshi_mid")):
      typ = "REAL"
    else:
      typ = "TEXT"
    out.append((f, typ))
  return tuple(out)


def range_band_migrations_pg() -> tuple[tuple[str, str], ...]:
  return tuple(
    (col, "DOUBLE PRECISION" if typ == "REAL" else "TEXT")
    for col, typ in range_band_migrations()
  )
