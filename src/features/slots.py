from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

FIFTEEN_MIN = timedelta(minutes=15)
DEFAULT_TZ = "America/New_York"


def get_tz(name: str | None = None) -> ZoneInfo:
  return ZoneInfo(name or DEFAULT_TZ)


def floor_to_15m(ts: datetime | pd.Timestamp, tz_name: str = DEFAULT_TZ) -> pd.Timestamp:
  """Align to slot start (:00, :15, :30, :45) in the given timezone; return UTC."""
  tz = get_tz(tz_name)
  t = pd.Timestamp(ts)
  if t.tzinfo is None:
    t = t.tz_localize("UTC")
  local = t.tz_convert(tz)
  minute = (local.minute // 15) * 15
  floored = local.replace(minute=minute, second=0, microsecond=0)
  return floored.tz_convert("UTC")


def slot_end(slot_start: datetime | pd.Timestamp, tz_name: str = DEFAULT_TZ) -> pd.Timestamp:
  return floor_to_15m(slot_start, tz_name) + FIFTEEN_MIN


def current_slot_start(
  now: datetime | pd.Timestamp | None = None,
  tz_name: str = DEFAULT_TZ,
) -> pd.Timestamp:
  return floor_to_15m(now or datetime.now(timezone.utc), tz_name)


def next_slot_start(now: datetime | pd.Timestamp | None = None, tz_name: str = DEFAULT_TZ) -> pd.Timestamp:
  return current_slot_start(now, tz_name) + FIFTEEN_MIN


def _fmt_et_clock(ts: pd.Timestamp) -> str:
  """e.g. 5:45 PM"""
  s = ts.strftime("%I:%M %p")
  return s[1:] if s.startswith("0") else s


def slot_label(start: datetime | pd.Timestamp, tz_name: str = DEFAULT_TZ) -> str:
  tz = get_tz(tz_name)
  s = pd.Timestamp(start)
  if s.tzinfo is None:
    s = s.tz_localize("UTC")
  s = s.tz_convert(tz)
  e = s + FIFTEEN_MIN
  return f"{_fmt_et_clock(s)} – {_fmt_et_clock(e)} ET"


def reference_price_at_slot(
  df_1m: pd.DataFrame | None,
  slot_start_utc: pd.Timestamp,
  fallback: float | None = None,
) -> float:
  """BTC price at t=0 (start of the 15m interval)."""
  if df_1m is not None and not df_1m.empty:
    df = df_1m.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    slot = pd.Timestamp(slot_start_utc)
    if slot.tzinfo is None:
      slot = slot.tz_localize("UTC")
    at_or_before = df[df["timestamp"] <= slot]
    if not at_or_before.empty:
      return float(at_or_before.iloc[-1]["close"])
    after = df[df["timestamp"] > slot]
    if not after.empty:
      return float(after.iloc[0]["open"])
  if fallback is not None:
    return float(fallback)
  raise ValueError("Cannot determine reference price at slot start")
