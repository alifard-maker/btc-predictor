from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

FIFTEEN_MIN = timedelta(minutes=15)
DEFAULT_TZ = "America/New_York"
DEFAULT_LIVE_TICK_WINDOW_SEC = 120


@dataclass(frozen=True)
class SlotReference:
  price: float
  source: str  # locked_tick | live_tick | prior_minute_close | slot_minute_open | fallback


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


def slot_times_match(
  pred_slot: datetime | pd.Timestamp | None,
  monitor_slot_key: str | pd.Timestamp | None,
  tz_name: str = DEFAULT_TZ,
) -> bool:
  """True when prediction slot_start aligns with monitor slot_start (ISO or Timestamp)."""
  if pred_slot is None or not monitor_slot_key:
    return False
  try:
    return floor_to_15m(pred_slot, tz_name) == floor_to_15m(pd.Timestamp(monitor_slot_key), tz_name)
  except (TypeError, ValueError):
    return False


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


def prior_minute_close_at_slot(
  df_1m: pd.DataFrame,
  slot_start_utc: pd.Timestamp,
) -> float | None:
  """Last 1m close before slot_start — trade price in the final seconds before t=0."""
  slot = pd.Timestamp(slot_start_utc)
  if slot.tzinfo is None:
    slot = slot.tz_localize("UTC")
  else:
    slot = slot.tz_convert("UTC")

  df = df_1m.copy()
  df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
  before = df[df["timestamp"] < slot]
  if before.empty:
    return None
  last = before.iloc[-1]
  gap_sec = (slot - last["timestamp"]).total_seconds()
  if gap_sec <= 60:
    return float(last["close"])
  return None


def reference_price_at_slot(
  df_1m: pd.DataFrame | None,
  slot_start_utc: pd.Timestamp,
  fallback: float | None = None,
  live_price: float | None = None,
  now_utc: pd.Timestamp | None = None,
  locked_tick: float | None = None,
  live_tick_window_sec: float = DEFAULT_LIVE_TICK_WINDOW_SEC,
) -> SlotReference:
  """BTC price at t=0 — prefers live tick at open, else last trade before the boundary."""
  slot = pd.Timestamp(slot_start_utc)
  if slot.tzinfo is None:
    slot = slot.tz_localize("UTC")
  else:
    slot = slot.tz_convert("UTC")

  if locked_tick is not None and locked_tick > 0:
    return SlotReference(float(locked_tick), "locked_tick")

  now = pd.Timestamp(now_utc or pd.Timestamp.now(tz="UTC"))
  if now.tzinfo is None:
    now = now.tz_localize("UTC")
  age_sec = (now - slot).total_seconds()

  # Coinbase last trade captured at/near slot open (best match to "price at t=0")
  if live_price is not None and 0 <= age_sec <= live_tick_window_sec:
    return SlotReference(float(live_price), "live_tick")

  if df_1m is not None and not df_1m.empty:
    prior = prior_minute_close_at_slot(df_1m, slot)
    if prior is not None:
      return SlotReference(prior, "prior_minute_close")

    df = df_1m.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    exact = df[df["timestamp"] == slot]
    if not exact.empty:
      return SlotReference(float(exact.iloc[0]["open"]), "slot_minute_open")

    after = df[df["timestamp"] > slot]
    if not after.empty:
      return SlotReference(float(after.iloc[0]["open"]), "slot_minute_open")

  if live_price is not None:
    return SlotReference(float(live_price), "live_tick")

  if fallback is not None:
    return SlotReference(float(fallback), "fallback")
  raise ValueError("Cannot determine reference price at slot start")
