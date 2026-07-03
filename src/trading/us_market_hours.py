"""US regular trading hours gate for index hourly bots."""

from __future__ import annotations

from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo


def _market_hours_cfg(cfg: dict[str, Any] | None) -> dict[str, Any]:
  raw = (cfg or {}).get("market_hours") or {}
  return {
    "enabled": bool(raw.get("enabled", True)),
    "timezone": str(raw.get("timezone", "America/New_York")),
    "open": str(raw.get("open", "09:30")),
    "close": str(raw.get("close", "16:00")),
    "weekdays_only": bool(raw.get("weekdays_only", True)),
  }


def _parse_hhmm(value: str) -> time:
  parts = str(value).split(":")
  hour = int(parts[0])
  minute = int(parts[1]) if len(parts) > 1 else 0
  return time(hour, minute)


def is_us_rth(
  now: datetime | None = None,
  *,
  cfg: dict[str, Any] | None = None,
) -> bool:
  """True during configured US RTH window (default Mon–Fri 9:30–16:00 ET)."""
  mh = _market_hours_cfg(cfg)
  if not mh["enabled"]:
    return True
  tz = ZoneInfo(mh["timezone"])
  now = now or datetime.now(tz)
  if now.tzinfo is None:
    now = now.replace(tzinfo=tz)
  else:
    now = now.astimezone(tz)
  if mh["weekdays_only"] and now.weekday() >= 5:
    return False
  open_t = _parse_hhmm(mh["open"])
  close_t = _parse_hhmm(mh["close"])
  cur = now.time()
  return open_t <= cur < close_t


def index_trading_allowed(cfg: dict[str, Any] | None, *, now: datetime | None = None) -> bool:
  """Gate index bots to US RTH when market_hours is configured."""
  return is_us_rth(now=now, cfg=cfg)
