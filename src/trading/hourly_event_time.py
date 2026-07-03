"""Hourly Kalshi event ticker parsing and leg/event alignment."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

_HOURLY_SUFFIX_RE = re.compile(r"^(\d{2})([A-Z]{3})(\d{2})(\d{2})$")
_INDEX_SUFFIX_RE = re.compile(r"^(\d{2})([A-Z]{3})(\d{2})H(\d{2})(\d{2})$")

_SPX_PREFIXES = ("KXINXU-", "KXINX-", "KXINXDUD-")
_NDX_PREFIXES = ("KXNASDAQ100U-", "KXNASDAQ100-", "KXNASDAQDUD-")
_CRYPTO_PREFIXES = ("KXBTCD-", "KXBTC-", "KXETHD-", "KXETH-")


def hourly_event_time_suffix(event_ticker: str) -> str | None:
  """Time slice after series prefix, e.g. KXBTCD-26JUN3004 → 26JUN3004."""
  parts = str(event_ticker).split("-", 1)
  if len(parts) != 2 or not parts[1]:
    return None
  return parts[1]


def market_ticker_event_ticker(market_ticker: str) -> str:
  """Parent event from a market ticker (KXBTCD-26JUN3017-T59749.99 → KXBTCD-26JUN3017)."""
  t = str(market_ticker)
  if "-" not in t:
    return t
  return t.rsplit("-", 1)[0]


def is_kalshi_hourly_event(event_ticker: str) -> bool:
  e = str(event_ticker)
  return e.startswith(_CRYPTO_PREFIXES + _SPX_PREFIXES + _NDX_PREFIXES)


def hourly_asset_for_event(event_ticker: str) -> str | None:
  """btc | eth | spx | ndx for Kalshi hourly series, else None."""
  e = str(event_ticker)
  if e.startswith(("KXBTCD-", "KXBTC-")):
    return "btc"
  if e.startswith(("KXETHD-", "KXETH-")):
    return "eth"
  if e.startswith(_SPX_PREFIXES):
    return "spx"
  if e.startswith(_NDX_PREFIXES):
    return "ndx"
  return None


def hourly_asset_for_ticker(ticker: str) -> str | None:
  return hourly_asset_for_event(market_ticker_event_ticker(ticker))


def hourly_fill_belongs_to_asset(ticker: str, asset: str) -> bool:
  """True when a market/event ticker belongs to the bot asset."""
  leg_asset = hourly_asset_for_ticker(ticker)
  if leg_asset is None:
    return True
  return leg_asset == str(asset).lower()


def _sibling_prefixes(event_ticker: str) -> tuple[str, ...]:
  e = str(event_ticker)
  if e.startswith("KXBTCD-"):
    return ("KXBTC-",)
  if e.startswith("KXETHD-"):
    return ("KXETH-",)
  if e.startswith("KXINXU-"):
    return ("KXINX-",)
  if e.startswith("KXNASDAQ100U-"):
    return ("KXNASDAQ100-",)
  return ()


def ticker_belongs_to_hourly_event(ticker: str, event_ticker: str) -> bool:
  """True when a market ticker belongs to an hourly event (threshold + range siblings)."""
  t = str(ticker)
  e = str(event_ticker)
  if t == e or t.startswith(f"{e}-"):
    return True
  if not is_kalshi_hourly_event(e):
    return True
  suffix = hourly_event_time_suffix(e)
  if not suffix:
    return True
  if not (_HOURLY_SUFFIX_RE.match(suffix) or _INDEX_SUFFIX_RE.match(suffix)):
    return True
  for prefix in _sibling_prefixes(e):
    root = f"{prefix}{suffix}"
    if t == root or t.startswith(f"{root}-"):
      return True
  return False


def _parse_suffix_to_local(suffix: str, *, tz_name: str = "America/New_York") -> datetime | None:
  m = _INDEX_SUFFIX_RE.match(suffix)
  if m:
    yy, mon, dd, hh, mm = m.groups()
    try:
      month = datetime.strptime(mon, "%b").month
      return datetime(
        2000 + int(yy),
        month,
        int(dd),
        int(hh),
        int(mm),
        0,
        tzinfo=ZoneInfo(tz_name),
      )
    except ValueError:
      return None
  m = _HOURLY_SUFFIX_RE.match(suffix)
  if not m:
    return None
  yy, mon, dd, hh = m.groups()
  try:
    month = datetime.strptime(mon, "%b").month
    return datetime(
      2000 + int(yy),
      month,
      int(dd),
      int(hh),
      0,
      0,
      tzinfo=ZoneInfo(tz_name),
    )
  except ValueError:
    return None


def hourly_event_settle_utc(
  event_ticker: str,
  *,
  tz_name: str = "America/New_York",
) -> datetime | None:
  """Settlement instant for a Kalshi hourly event suffix (Eastern)."""
  suffix = hourly_event_time_suffix(event_ticker)
  if not suffix:
    return None
  local = _parse_suffix_to_local(suffix, tz_name=tz_name)
  if local is None:
    return None
  return local.astimezone(timezone.utc)


def hourly_event_has_settled(
  event_ticker: str,
  *,
  now: datetime | None = None,
) -> bool:
  settle = hourly_event_settle_utc(event_ticker)
  if settle is None:
    return False
  now = now or datetime.now(timezone.utc)
  return now >= settle


def should_rollover_close_hourly_leg(
  pos: dict[str, Any],
  prev_period_key: str,
  *,
  now: datetime | None = None,
) -> bool:
  """Only rollover-close legs that belong to prev_period and whose settle time has passed."""
  ticker = str(pos.get("market_ticker") or "")
  if not ticker_belongs_to_hourly_event(ticker, prev_period_key):
    return False
  leg_event = market_ticker_event_ticker(ticker)
  settle = hourly_event_settle_utc(leg_event)
  if settle is None:
    return ticker_belongs_to_hourly_event(ticker, prev_period_key)
  now = now or datetime.now(timezone.utc)
  return now >= settle
