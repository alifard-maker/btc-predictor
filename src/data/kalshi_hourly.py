"""Kalshi hourly/daily threshold market settlement."""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from src.calibration.sources import KALSHI_EXIT_SOURCE
from src.data.kalshi import KalshiClient
from src.db.hourly_store import HourlyResolution

log = logging.getLogger(__name__)


def _contract_yes_outcome(
  settle_brti: float,
  *,
  strike_type: str,
  floor_strike: float | None,
  cap_strike: float | None,
) -> int:
  st = (strike_type or "").lower()
  if st == "greater" and floor_strike is not None:
    return 1 if settle_brti >= float(floor_strike) else 0
  if st == "less" and floor_strike is not None:
    return 1 if settle_brti < float(floor_strike) else 0
  if st == "between" and floor_strike is not None and cap_strike is not None:
    lo, hi = float(floor_strike), float(cap_strike)
    return 1 if lo <= settle_brti <= hi else 0
  return 0


def fetch_market_row(kalshi: KalshiClient, ticker: str) -> dict[str, Any] | None:
  try:
    data = kalshi.get(f"/markets/{ticker}")
    return data.get("market") or data
  except Exception as e:
    log.warning("Kalshi market fetch %s failed: %s", ticker, e)
    return None


def market_settled(row: dict[str, Any]) -> bool:
  status = str(row.get("status", ""))
  exp = row.get("expiration_value")
  if exp in (None, ""):
    return False
  return status not in ("active", "open", "unopened", "inactive")


def resolve_primary_contract(row: dict[str, Any], settle_brti: float) -> HourlyResolution:
  ref = float(row.get("reference_price") or 0)
  outcome = _contract_yes_outcome(
    settle_brti,
    strike_type=str(row.get("primary_strike_type") or ""),
    floor_strike=row.get("primary_floor"),
    cap_strike=row.get("primary_cap"),
  )
  actual_return = (settle_brti - ref) / ref if ref > 0 else 0.0
  return HourlyResolution(
    settle_brti=settle_brti,
    outcome=outcome,
    actual_return=actual_return,
    exit_source=KALSHI_EXIT_SOURCE,
  )


def try_resolve_pending(kalshi: KalshiClient, pending: dict[str, Any]) -> HourlyResolution | None:
  ticker = pending.get("primary_ticker")
  if not ticker:
    return None
  mrow = fetch_market_row(kalshi, str(ticker))
  if not mrow or not market_settled(mrow):
    return None
  exp = mrow.get("expiration_value")
  if exp in (None, ""):
    return None
  settle_brti = float(exp)
  return resolve_primary_contract(pending, settle_brti)
