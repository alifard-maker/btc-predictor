"""Tests for hourly settlement at period rollover."""

from __future__ import annotations

import tempfile
from pathlib import Path

from src.trading.hourly_bot import HourlyBot
from src.trading.hourly_bot_store import HourlyBotSettings, HourlyBotStore
from src.trading.hourly_settlement import (
  contract_spec_from_label,
  settlement_exit_cents,
  yes_wins_at_settle,
)


def test_yes_wins_inside_range_band():
  spec = contract_spec_from_label("NO · $1,610 to 1,629.99")
  assert yes_wins_at_settle(1620.0, spec) is True
  assert yes_wins_at_settle(1577.0, spec) is False


def test_settlement_exit_cents_winning_no_far_from_band():
  spec = contract_spec_from_label("NO · $1,610 to 1,629.99")
  assert settlement_exit_cents(side="no", settle_price=1577.56, spec=spec) == 100
  assert settlement_exit_cents(side="yes", settle_price=1577.56, spec=spec) == 0


def test_settlement_exit_cents_or_above_threshold():
  spec = contract_spec_from_label("$1,590 or above")
  assert settlement_exit_cents(side="no", settle_price=1577.0, spec=spec) == 100
  assert settlement_exit_cents(side="yes", settle_price=1600.0, spec=spec) == 100


def test_hourly_rollover_settles_winning_no_not_at_mark():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "hourly_bot_eth.db")
    store.save_settings(HourlyBotSettings(enabled=True, max_spend_per_hour_usd=100.0, mode="paper"))
    prev_event = "KXETH-26JUN291200"
    new_event = "KXETH-26JUN291300"
    pos = store.open_position({
      "event_ticker": prev_event,
      "market_ticker": "KXETH-26JUN291200-T1610",
      "side": "no",
      "contracts": 14,
      "entry_price_cents": 91,
      "cost_usd": 12.74,
      "label": "NO · $1,610 to 1,629.99",
    })
    store.update_position_mark(pos["id"], 80)
    store.sync_period(prev_event, store.get_settings())

    bot = HourlyBot(store, asset="eth")
    tab = {
      "ok": True,
      "brti_live": 1577.56,
      "event": {"event_ticker": new_event},
      "live": {
        "current_price": 1577.56,
        "index_id": "ERTI",
        "strategy_range": {"contracts": []},
        "strategy_threshold": {"contracts": []},
      },
    }
    actions = bot.run_continuous_cycle(tab)
    exits = [a for a in actions if a.get("action") == "exit"]
    assert len(exits) == 1
    assert exits[0]["exit_price_cents"] == 100
    assert exits[0]["pnl_usd"] == round(14 * (100 - 91) / 100.0, 2)
    assert "PERIOD SETTLEMENT" in exits[0]["detail"]
    assert store.open_positions(prev_event) == []


def test_hourly_rollover_detail_says_live_for_live_positions():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "hourly_bot_btc.db")
    store.save_settings(HourlyBotSettings(enabled=True, max_spend_per_hour_usd=100.0, mode="live"))
    prev_event = "KXBTCD-26JUN2923"
    new_event = "KXBTCD-26JUN3000"
    pos = store.open_position({
      "event_ticker": prev_event,
      "market_ticker": "KXBTCD-T598",
      "side": "no",
      "contracts": 2,
      "entry_price_cents": 70,
      "cost_usd": 1.40,
      "label": "$59,800 to 59,899.99",
      "mode": "live",
    })
    store.update_position_mark(pos["id"], 50)
    store.sync_period(prev_event, store.get_settings())

    bot = HourlyBot(store, asset="btc")
    tab = {
      "ok": True,
      "brti_live": 59869.81,
      "event": {"event_ticker": new_event},
      "live": {
        "current_price": 59869.81,
        "index_id": "BRTI",
        "strategy_range": {"contracts": []},
        "strategy_threshold": {"contracts": []},
      },
    }
    actions = bot.run_continuous_cycle(tab)
    exits = [a for a in actions if a.get("action") == "exit"]
    assert len(exits) == 1
    assert exits[0]["mode"] == "live"
    assert exits[0]["detail"].startswith("Live EXIT (PERIOD SETTLEMENT):")
