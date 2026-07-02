"""Purge foreign-asset phantom legs from hourly bot stores."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from src.trading.hourly_bot_store import HourlyBotStore
from src.trading.live_position_sync import purge_foreign_asset_open_positions


def test_purge_foreign_asset_closes_btc_legs_in_eth_store():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "eth.db")
    store.open_position({
      "id": "phantom-1",
      "event_ticker": "KXBTCD-26JUL0214",
      "market_ticker": "KXBTCD-26JUL0214-T61599.99",
      "side": "yes",
      "contracts": 2,
      "entry_price_cents": 50,
      "cost_usd": 1.0,
      "mode": "live",
    })
    kalshi = MagicMock()
    kalshi.authenticated = True
    out = purge_foreign_asset_open_positions(store, kalshi, asset="eth")
    assert len(out["changes"]) == 1
    assert out["changes"][0]["action"] == "purged_foreign_asset"
    assert store.all_open_live_positions() == []
