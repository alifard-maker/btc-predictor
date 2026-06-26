from __future__ import annotations

from typing import Any

import pandas as pd

from src.calibration.sources import is_kalshi_consistent
from src.calibration.tracker import CalibrationTracker
from src.config import load_config
from src.calibration.sources import KALSHI_EXIT_SOURCE, KALSHI_REF_SOURCE
from src.data.kalshi import KalshiClient
from src.db.store import PredictionResolution
from src.features.slots import floor_to_15m


def backfill_kalshi_predictions(
  cfg: dict[str, Any] | None = None,
  *,
  dry_run: bool = False,
  limit: int | None = None,
) -> dict[str, int]:
  """Re-label and re-resolve predictions using Kalshi KXBTC15M settlement data."""
  cfg = cfg or load_config()
  tracker = CalibrationTracker(cfg)
  kalshi = KalshiClient(cfg)
  tz = cfg.get("timezone", "America/New_York")

  df = tracker.load_all()
  if limit:
    df = df.tail(int(limit))

  stats = {
    "examined": 0,
    "updated": 0,
    "skipped_no_market": 0,
    "skipped_unsettled": 0,
    "kalshi_consistent": 0,
  }
  if df.empty:
    return stats

  updates: dict[str, PredictionResolution] = {}
  for row in df.itertuples(index=False):
    stats["examined"] += 1
    ts = pd.to_datetime(getattr(row, "timestamp"), utc=True)
    slot_s = floor_to_15m(ts, tz)
    settlement = kalshi.slot_settlement(slot_s)
    if settlement is None:
      stats["skipped_no_market"] += 1
      continue
    if not settlement.settled:
      stats["skipped_unsettled"] += 1
      continue

    resolution = kalshi.resolution_for_entry(float(getattr(row, "price")), settlement)
    if resolution is None:
      stats["skipped_unsettled"] += 1
      continue

    exit_price, actual_return, outcome = resolution
    ts_key = ts.isoformat()
    ref_source = KALSHI_REF_SOURCE
    if getattr(row, "reference_source", None) in (None, "", "exchange_legacy", "fallback"):
      ref_source = "kalshi_backfill"

    updates[ts_key] = PredictionResolution(
      exit_price=exit_price,
      actual_return=actual_return,
      exit_source=KALSHI_EXIT_SOURCE,
      outcome=outcome,
      reference_price=settlement.open_brti,
      reference_source=ref_source,
      kalshi_market_ticker=settlement.market_ticker,
    )

  if dry_run:
    stats["updated"] = len(updates)
    stats["kalshi_consistent"] = len(updates)
    return stats

  if updates:
    stats["updated"] = tracker.resolve_with_prices(updates, force=True)

  resolved = tracker.load_resolved()
  if not resolved.empty and "reference_source" in resolved.columns:
    mask = resolved.apply(
      lambda r: is_kalshi_consistent(r.get("reference_source"), r.get("exit_source")),
      axis=1,
    )
    stats["kalshi_consistent"] = int(mask.sum())

  return stats
