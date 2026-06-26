"""Backfill missed late-entry signals from post-mortems and 1m replay."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from src.calibration.tracker import CalibrationTracker
from src.config import load_config
from src.data.storage import CandleStorage
from src.features.slots import floor_to_15m
from src.trading.late_entry import LateEntryAdvisor


def _load_postmortems(path: Path) -> list[dict[str, Any]]:
  if not path.exists():
    return []
  out: list[dict[str, Any]] = []
  for line in path.read_text().strip().splitlines():
    try:
      out.append(json.loads(line))
    except json.JSONDecodeError:
      continue
  return out


def _replay_late_entry(
  advisor: LateEntryAdvisor,
  df_1m: pd.DataFrame | None,
  *,
  slot_start: pd.Timestamp,
  reference_price: float,
  original_prob_up: float,
) -> tuple[str, float, int] | None:
  """Walk minute-by-minute through the slot; return first LATE LONG/SHORT."""
  if df_1m is None or df_1m.empty or reference_price <= 0:
    return None

  df = df_1m.copy()
  df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
  slot_end = slot_start + pd.Timedelta(minutes=15)

  for minute in range(2, 15):
    as_of = slot_start + pd.Timedelta(minutes=minute)
    if as_of >= slot_end:
      break
    in_slot = df[(df["timestamp"] >= slot_start) & (df["timestamp"] < as_of)].sort_values("timestamp")
    if len(in_slot) < 2:
      continue

    current_price = float(in_slot["close"].iloc[-1])
    elapsed_min = minute
    seconds_remaining = int((slot_end - as_of).total_seconds())
    stats = advisor.slot_path_stats(
      df,
      slot_start,
      reference_price,
      momentum_bars=advisor.momentum_bars,
      recovery_bars=advisor.recovery_recent_bars,
    )
    decision = advisor.evaluate(
      elapsed_minutes=float(elapsed_min),
      seconds_remaining=seconds_remaining,
      reference_price=reference_price,
      stats=stats,
      original_prob_up=original_prob_up,
      current_price=current_price,
    )
    if decision.action in ("LATE LONG", "LATE SHORT"):
      return decision.action, float(decision.prob_up), seconds_remaining
  return None


def backfill_late_entries(
  cfg: dict[str, Any] | None = None,
  *,
  dry_run: bool = False,
  force: bool = False,
  replay: bool = True,
) -> dict[str, int]:
  """Fill missing late_entry_* fields on NO TRADE rows."""
  cfg = cfg or load_config()
  tracker = CalibrationTracker(cfg)
  tz = cfg.get("timezone", "America/New_York")
  advisor = LateEntryAdvisor(cfg)
  storage = CandleStorage(cfg)
  df_1m = storage.load("1m")

  postmortem_path = Path(cfg["paths"]["logs"]) / "postmortems.jsonl"
  postmortems = _load_postmortems(postmortem_path)
  pm_by_ts: dict[str, dict[str, Any]] = {}
  for pm in postmortems:
    ts = pm.get("timestamp")
    if ts:
      pm_by_ts[pd.Timestamp(ts, tz="UTC").isoformat()] = pm

  df = tracker.load_resolved()
  if df.empty:
    return {
      "examined": 0,
      "from_postmortem": 0,
      "from_replay": 0,
      "updated": 0,
      "skipped_has_late": 0,
      "skipped_not_no_trade": 0,
    }

  stats = {
    "examined": 0,
    "from_postmortem": 0,
    "from_replay": 0,
    "updated": 0,
    "skipped_has_late": 0,
    "skipped_not_no_trade": 0,
  }

  for row in df.itertuples(index=False):
    stats["examined"] += 1
    signal = str(getattr(row, "signal", ""))
    if signal != "NO TRADE":
      stats["skipped_not_no_trade"] += 1
      continue

    late_sig = str(getattr(row, "late_entry_signal", "") or "")
    if late_sig and not force:
      stats["skipped_has_late"] += 1
      continue

    ts = pd.Timestamp(getattr(row, "timestamp"), tz="UTC")
    ts_key = ts.isoformat()
    slot_s = floor_to_15m(ts, tz)
    ref = float(getattr(row, "price"))
    prob_up = float(getattr(row, "prob_up", 0.5))

    action = ""
    late_prob = 0.5
    seconds_remaining = 0
    source = ""

    pm = pm_by_ts.get(ts_key)
    if pm and pm.get("late_entry_signal"):
      action = str(pm["late_entry_signal"])
      late_prob = float(pm.get("late_entry_prob_up") or prob_up)
      seconds_remaining = int(pm.get("late_entry_seconds_remaining") or 300)
      source = "postmortem"
    elif replay:
      hit = _replay_late_entry(
        advisor,
        df_1m,
        slot_start=slot_s,
        reference_price=ref,
        original_prob_up=prob_up,
      )
      if hit:
        action, late_prob, seconds_remaining = hit
        source = "replay"

    if not action:
      continue

    if dry_run:
      stats["updated"] += 1
      if source == "postmortem":
        stats["from_postmortem"] += 1
      elif source == "replay":
        stats["from_replay"] += 1
      continue

    ok = tracker.store.backfill_late_entry(
      ts_key,
      action,
      late_prob,
      seconds_remaining,
      force=force or bool(late_sig),
    )
    if ok:
      stats["updated"] += 1
      if source == "postmortem":
        stats["from_postmortem"] += 1
      elif source == "replay":
        stats["from_replay"] += 1

  return stats
