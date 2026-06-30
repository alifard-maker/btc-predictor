"""Tests for hourly active_book event selection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from src.data.kalshi_daily import DailyEventBook, KalshiContractMarket, KalshiDailyMarkets


def _market(
  *,
  event_ticker: str,
  series: str,
  close_time: datetime,
) -> KalshiContractMarket:
  return KalshiContractMarket(
    ticker=f"{event_ticker}-T1",
    event_ticker=event_ticker,
    title=event_ticker,
    strike_type="greater",
    floor_strike=60_000.0,
    cap_strike=None,
    close_time=close_time,
    open_time=close_time - timedelta(hours=1),
    yes_bid=0.5,
    yes_ask=0.52,
    subtitle="",
    series_ticker=series,
  )


def test_active_book_picks_soonest_hourly_event_not_best_bracket():
  now = datetime.now(timezone.utc)
  soon_close = now + timedelta(hours=1)
  far_close = now + timedelta(hours=12)

  soon_thresh = _market(event_ticker="KXBTCD-26JUN3006", series="KXBTCD", close_time=soon_close)
  far_thresh = _market(event_ticker="KXBTCD-26JUN3017", series="KXBTCD", close_time=far_close)

  cfg = {
    "kalshi": {"enabled": False},
    "daily": {
      "discovery_cache_sec": 0,
      "threshold_series": ["KXBTCD"],
      "range_series": ["KXBTC"],
    },
  }
  markets = KalshiDailyMarkets(cfg)
  markets._fetch_open_events = MagicMock(
    return_value=[
      {"event_ticker": "KXBTCD-26JUN3017", "strike_date": far_close.isoformat()},
      {"event_ticker": "KXBTCD-26JUN3006", "strike_date": soon_close.isoformat()},
    ],
  )

  def _fetch_event_markets(series: str, event_ticker: str):
    if event_ticker == "KXBTCD-26JUN3006":
      return [soon_thresh]
    if event_ticker == "KXBTCD-26JUN3017":
      return [far_thresh]
    return []

  markets._fetch_event_markets = MagicMock(side_effect=_fetch_event_markets)
  markets._ranges_for_threshold_event = MagicMock(return_value=[])

  book = markets.active_book(reference_price=59_200.0)
  assert book is not None
  assert book.event_ticker == "KXBTCD-26JUN3006"
