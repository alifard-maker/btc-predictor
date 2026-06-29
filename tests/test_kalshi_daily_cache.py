"""Tests for Kalshi daily market discovery caching."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.data.kalshi_daily import KalshiDailyMarkets


def test_fetch_open_events_uses_cache_within_ttl():
  cfg = {"kalshi": {"enabled": False}, "daily": {"discovery_cache_sec": 75}}
  markets = KalshiDailyMarkets(cfg)
  markets.kalshi.get = MagicMock(return_value={"events": [{"event_ticker": "KXBTCD-1"}], "cursor": None})

  first = markets._fetch_open_events("KXBTCD")
  second = markets._fetch_open_events("KXBTCD")

  assert first == second
  markets.kalshi.get.assert_called_once()


def test_active_book_uses_cache_within_ttl():
  cfg = {"kalshi": {"enabled": False}, "daily": {"discovery_cache_sec": 75, "threshold_series": ["KXBTCD"], "range_series": ["KXBTC"]}}
  markets = KalshiDailyMarkets(cfg)
  markets._fetch_open_events = MagicMock(return_value=[])
  markets.active_book(reference_price=100_000.0)
  markets.active_book(reference_price=100_000.0)
  markets._fetch_open_events.assert_called_once()
