"""Tests for forced position close on hour/slot rollover."""

from __future__ import annotations

import tempfile
from pathlib import Path

from src.trading.slot15_bot import Slot15Bot
from src.trading.slot15_bot_store import Slot15BotSettings, Slot15BotStore
from tests.test_slot15_bot_continuous import _live_tab


def test_slot15_closes_orphan_position_on_slot_rollover():
  with tempfile.TemporaryDirectory() as tmp:
    store = Slot15BotStore(Path(tmp) / "bot.db")
    store.save_settings(Slot15BotSettings(enabled=True, max_spend_per_slot_usd=25.0, mode="paper"))
    prev_slot = "2025-06-28T12:00:00-04:00"
    new_slot = "2025-06-28T12:15:00-04:00"
    pos = store.open_position({
      "event_ticker": prev_slot,
      "market_ticker": "KXBTC15M-OLD",
      "side": "no",
      "contracts": 36,
      "entry_price_cents": 68,
      "cost_usd": 24.48,
    })
    store.update_position_mark(pos["id"], 80)
    store.sync_period(prev_slot, store.get_settings())
    bot = Slot15Bot(store, asset="btc")
    tab = _live_tab(slot_key=new_slot)
    tab["kalshi"]["market_ticker"] = "KXBTC15M-NEW"
    actions = bot.run_continuous_cycle(tab)
    exits = [a for a in actions if a.get("action") == "exit"]
    assert len(exits) == 1
    assert exits[0]["event_ticker"] == prev_slot
    assert exits[0]["trigger"] == "period_rollover"
    assert exits[0]["pnl_usd"] == round(36 * (68 - 80) / 100.0, 2)
    assert store.open_positions(prev_slot) == []
