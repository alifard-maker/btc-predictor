"""Tests for hourly event time parsing and rollover guards."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from src.trading.hourly_event_time import (
  hourly_event_has_settled,
  hourly_event_settle_utc,
  should_rollover_close_hourly_leg,
  ticker_belongs_to_hourly_event,
)


def test_ticker_belongs_to_hourly_event_siblings():
  ev = "KXBTCD-26JUN3005"
  assert ticker_belongs_to_hourly_event("KXBTCD-26JUN3005-T59499.99", ev)
  assert ticker_belongs_to_hourly_event("KXBTC-26JUN3005-B59450", ev)
  assert not ticker_belongs_to_hourly_event("KXBTCD-26JUN3017-T59749.99", ev)


def test_hourly_event_settle_utc_parses_suffix():
  settle = hourly_event_settle_utc("KXBTCD-26JUN3017")
  assert settle is not None
  et = settle.astimezone(ZoneInfo("America/New_York"))
  assert et.year == 2026
  assert et.month == 6
  assert et.day == 30
  assert et.hour == 17


def test_should_not_rollover_close_future_hour_leg():
  pos = {
    "market_ticker": "KXBTCD-26JUN3017-T59749.99",
    "side": "no",
  }
  now = datetime(2026, 6, 30, 9, 16, tzinfo=timezone.utc)  # ~5:16 AM ET
  assert not should_rollover_close_hourly_leg(pos, "KXBTCD-26JUN3017", now=now)


def test_should_rollover_close_after_settle():
  pos = {
    "market_ticker": "KXBTCD-26JUN3005-T59499.99",
    "side": "no",
  }
  settle = hourly_event_settle_utc("KXBTCD-26JUN3005")
  assert settle is not None
  now = settle
  assert hourly_event_has_settled("KXBTCD-26JUN3005", now=now)
  assert should_rollover_close_hourly_leg(pos, "KXBTCD-26JUN3005", now=now)


def test_skips_rollover_when_ticker_wrong_period():
  pos = {"market_ticker": "KXBTCD-26JUN3017-T59749.99", "side": "no"}
  assert not should_rollover_close_hourly_leg(pos, "KXBTCD-26JUN3006")
