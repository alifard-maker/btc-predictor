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
  live_price: float | None = None,
  now_utc: pd.Timestamp | None = None,
) -> float:
  """BTC price at t=0 (open of the 1m candle that starts at slot_start)."""
  slot = pd.Timestamp(slot_start_utc)
  if slot.tzinfo is None:
    slot = slot.tz_localize("UTC")
  else:
    slot = slot.tz_convert("UTC")

  if df_1m is not None and not df_1m.empty:
    df = df_1m.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

    # 1m OHLC timestamps are candle open times — use open at exact slot start
    exact = df[df["timestamp"] == slot]
    if not exact.empty:
      return float(exact.iloc[0]["open"])

    # Close of the prior minute ends exactly at slot_start
    before = df[df["timestamp"] < slot]
    if not before.empty:
      last = before.iloc[-1]
      gap_sec = (slot - last["timestamp"]).total_seconds()
      if gap_sec <= 60:
        return float(last["close"])

    after = df[df["timestamp"] > slot]
    if not after.empty:
      return float(after.iloc[0]["open"])

  # Slot just opened but 1m candle not in store yet — use live ticker if fresh
  if live_price is not None:
    now = pd.Timestamp(now_utc or pd.Timestamp.now(tz="UTC"))
    if now.tzinfo is None:
      now = now.tz_localize("UTC")
    age_sec = (now - slot).total_seconds()
    if 0 <= age_sec <= 90:
      return float(live_price)

  if fallback is not None:
    return float(fallback)
  raise ValueError("Cannot determine reference price at slot start")
