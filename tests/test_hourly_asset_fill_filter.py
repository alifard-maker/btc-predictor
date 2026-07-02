"""Kalshi fill sync must not cross-contaminate BTC and ETH hourly bot stores."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from src.trading.hourly_bot_store import HourlyBotStore
from src.trading.kalshi_fill_sync import backfill_kalshi_hourly_fills


def _kalshi_with_fills(fills):
  kalshi = MagicMock()
  kalshi.authenticated = True
  kalshi.list_fills.return_value = fills
  return kalshi


def test_eth_backfill_ignores_btc_hourly_fills():
  btc_ticker = "KXBTCD-26JUL0214-T61599.99"
  eth_ticker = "KXETHD-26JUL0216-T1700"
  fills = [
    {
      "order_id": "btc-buy",
      "ticker": btc_ticker,
      "action": "buy",
      "side": "yes",
      "yes_price": 50,
      "count": 2,
      "created_time": "2026-07-02T20:00:00+00:00",
    },
    {
      "order_id": "eth-buy",
      "ticker": eth_ticker,
      "action": "buy",
      "side": "yes",
      "yes_price": 40,
      "count": 1,
      "created_time": "2026-07-02T20:01:00+00:00",
    },
  ]
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "eth.db")
    kalshi = _kalshi_with_fills(fills)
    out = backfill_kalshi_hourly_fills(store, kalshi, force=True, asset="eth")
    assert out["ok"] is True
    trades = store.list_trades(limit=20)
    tickers = {t.get("market_ticker") for t in trades}
    assert btc_ticker not in tickers
    assert eth_ticker in tickers
