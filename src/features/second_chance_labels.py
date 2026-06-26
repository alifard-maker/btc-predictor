"""Training rows for 2nd Chance — features available at minute 4 of each 15m slot."""

from __future__ import annotations

from typing import Any

import pandas as pd

from src.features.slots import floor_to_15m
from src.trading.late_entry import LateEntryAdvisor


def build_second_chance_training_rows(
  df_1m: pd.DataFrame,
  *,
  tz_name: str = "America/New_York",
  elapsed_minutes: float = 4.0,
  open_probs: pd.Series | None = None,
) -> pd.DataFrame:
  """
  One row per complete 15m slot with label = 1 if slot close > t=0 reference.
  Intra-slot stats use only the first `elapsed_minutes` of 1m bars.
  """
  if df_1m is None or df_1m.empty:
    return pd.DataFrame()

  df = df_1m.copy()
  df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
  df = df.sort_values("timestamp")
  df["_slot"] = df["timestamp"].apply(lambda t: floor_to_15m(t, tz_name))

  rows: list[dict[str, Any]] = []
  bars_needed = max(2, int(elapsed_minutes))

  for slot_s, grp in df.groupby("_slot"):
    grp = grp.sort_values("timestamp")
    if len(grp) < bars_needed + 2:
      continue
    ref = float(grp.iloc[0]["close"])
    if ref <= 0:
      continue
    cutoff = grp.iloc[min(bars_needed, len(grp) - 1)]["timestamp"]
    early = grp[grp["timestamp"] <= cutoff]
    if len(early) < 2:
      continue
    settle = float(grp.iloc[-1]["close"])
    label = int(settle > ref)

    stats = LateEntryAdvisor.slot_path_stats(
      early,
      pd.Timestamp(slot_s),
      ref,
      momentum_bars=4,
      recovery_bars=4,
    )
    slot_key = pd.Timestamp(slot_s).isoformat()
    open_prob = 0.5
    if open_probs is not None and slot_key in open_probs.index:
      open_prob = float(open_probs[slot_key])

    rows.append({
      "timestamp": slot_s,
      "open_prob_up": open_prob,
      "open_signal_long": int(open_prob >= 0.57),
      "open_signal_short": int(open_prob <= 0.43),
      "gap_pct": stats.gap_pct,
      "pct_time_above_ref": stats.pct_time_above_ref,
      "ref_crossings": stats.ref_crossings,
      "slot_mom_pct": stats.slot_mom_pct,
      "recent_mom_pct": stats.recent_mom_pct,
      "recent_above_ref_pct": stats.recent_above_ref_pct,
      "elapsed_minutes": elapsed_minutes,
      "minutes_remaining": 15.0 - elapsed_minutes,
      "label": label,
    })

  return pd.DataFrame(rows)


def second_chance_feature_columns() -> list[str]:
  return [
    "open_prob_up",
    "open_signal_long",
    "open_signal_short",
    "gap_pct",
    "pct_time_above_ref",
    "ref_crossings",
    "slot_mom_pct",
    "recent_mom_pct",
    "recent_above_ref_pct",
    "elapsed_minutes",
    "minutes_remaining",
  ]
