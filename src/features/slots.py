from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

FIFTEEN_MIN = timedelta(minutes=15)


def floor_to_15m(ts: datetime | pd.Timestamp) -> pd.Timestamp:
  """Align timestamp to slot start (:00, :15, :30, :45)."""
  ts = pd.Timestamp(ts)
  if ts.tzinfo is None:
    ts = ts.tz_localize("UTC")
  else:
    ts = ts.tz_convert("UTC")
  minute = (ts.minute // 15) * 15
  return ts.replace(minute=minute, second=0, microsecond=0)


def slot_end(slot_start: datetime | pd.Timestamp) -> pd.Timestamp:
  return floor_to_15m(slot_start) + FIFTEEN_MIN


def current_slot_start(now: datetime | pd.Timestamp | None = None) -> pd.Timestamp:
  return floor_to_15m(now or datetime.now(timezone.utc))


def next_slot_start(now: datetime | pd.Timestamp | None = None) -> pd.Timestamp:
  return current_slot_start(now) + FIFTEEN_MIN


def slot_label(start: datetime | pd.Timestamp) -> str:
  s = floor_to_15m(start)
  e = s + FIFTEEN_MIN
  return f"{s.strftime('%H:%M')}–{e.strftime('%H:%M')} UTC"


def align_df_to_slots(df: pd.DataFrame) -> pd.DataFrame:
  """Keep one row per completed 15m slot (merge_asof to slot boundaries)."""
  if df.empty:
    return df
  out = df.copy()
  out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
  out["slot_start"] = out["timestamp"].apply(floor_to_15m)
  # Prefer row whose timestamp matches slot close (or last in slot)
  out = out.sort_values("timestamp").groupby("slot_start", as_index=False).last()
  out["timestamp"] = out["slot_start"]
  return out.drop(columns=["slot_start"], errors="ignore")
