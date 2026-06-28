"""Persistent paper bankroll across hours/slots."""

from __future__ import annotations

import tempfile
from pathlib import Path

from src.trading.hourly_bot import HourlyBot
from src.trading.hourly_bot_store import HourlyBotSettings, HourlyBotStore
from src.trading.slot15_bot import Slot15Bot
from src.trading.slot15_bot_store import Slot15BotSettings, Slot15BotStore


def test_paper_win_increases_bankroll_and_persists_across_hours():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    settings = HourlyBotSettings(enabled=True, max_spend_per_hour_usd=25.0, mode="paper")
    store.save_settings(settings)
    store.log_trade({
      "event_ticker": "EV1",
      "action": "exit",
      "mode": "paper",
      "status": "filled",
      "pnl_usd": 5.0,
    })
    assert store.hour_bankroll_usd("EV1", 25.0, settings) == 30.0
    assert store.hour_bankroll_usd("EV2", 25.0, settings) == 30.0
    assert store.realized_pnl_usd("EV2") == 0.0


def test_reset_paper_bankroll_restores_max_cap():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    settings = HourlyBotSettings(max_spend_per_hour_usd=25.0, mode="paper")
    store.save_settings(settings)
    store.log_trade({
      "event_ticker": "EV1",
      "action": "exit",
      "mode": "paper",
      "status": "filled",
      "pnl_usd": 8.0,
    })
    assert store.hour_bankroll_usd("EV1", 25.0, settings) == 33.0
    paper = store.reset_paper_bankroll(25.0)
    assert paper["paper_bankroll_usd"] == 25.0
    assert paper["paper_realized_all_time_usd"] == 0.0
    assert store.get_settings().auto_stopped is False
    assert store.hour_bankroll_usd("EV1", 25.0, settings) == 25.0


def test_live_mode_interval_bankroll_unaffected_by_paper_state():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    paper_settings = HourlyBotSettings(max_spend_per_hour_usd=25.0, mode="paper")
    store.save_settings(paper_settings)
    store.log_trade({
      "event_ticker": "EV1",
      "action": "exit",
      "mode": "paper",
      "status": "filled",
      "pnl_usd": 10.0,
    })
    live_settings = HourlyBotSettings(max_spend_per_hour_usd=25.0, mode="live")
    store.save_settings(live_settings)
    assert store.hour_bankroll_usd("EV2", 25.0, live_settings) == 25.0
    assert store.get_paper_state_dict(25.0)["paper_bankroll_usd"] == 35.0
    store.log_trade({
      "event_ticker": "EV2",
      "action": "exit",
      "mode": "live",
      "status": "filled",
      "pnl_usd": 4.0,
    })
    assert store.hour_bankroll_usd("EV2", 25.0, live_settings) == 29.0


def test_paper_auto_stop_clears_on_new_hour():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.save_settings(HourlyBotSettings(
      enabled=True, auto_stopped=True, max_spend_per_hour_usd=25.0, mode="paper",
    ))
    store.sync_period("EV1", store.get_settings())
    settings, _ = store.sync_period("EV2", store.get_settings())
    assert settings.auto_stopped is False


def test_slot15_paper_bankroll_persists_across_slots():
  with tempfile.TemporaryDirectory() as tmp:
    store = Slot15BotStore(Path(tmp) / "bot.db")
    settings = Slot15BotSettings(max_spend_per_slot_usd=20.0, mode="paper")
    store.save_settings(settings)
    store.log_trade({
      "event_ticker": "SLOT1",
      "action": "exit",
      "mode": "paper",
      "status": "filled",
      "pnl_usd": 3.0,
    })
    assert store.slot_bankroll_usd("SLOT2", 20.0, settings) == 23.0
    paper = store.reset_paper_bankroll(20.0)
    assert paper["paper_bankroll_usd"] == 20.0


def test_paper_status_includes_bankroll_fields():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.save_settings(HourlyBotSettings(max_spend_per_hour_usd=25.0, mode="paper"))
    store.log_trade({
      "event_ticker": "EV1",
      "action": "exit",
      "mode": "paper",
      "status": "filled",
      "pnl_usd": 5.0,
    })
    st = store.status("EV1")
    assert st["paper_bankroll"]["paper_bankroll_usd"] == 30.0
    assert st["paper_bankroll"]["paper_bankroll_since_reset_usd"] == 5.0


def test_fresh_start_clears_trades_and_resets_bankroll():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.log_trade({
      "event_ticker": "EV1",
      "action": "enter",
      "mode": "paper",
      "status": "filled",
      "cost_usd": 10.0,
    })
    store.open_position({
      "id": "p1",
      "event_ticker": "EV1",
      "market_ticker": "T1",
      "side": "yes",
      "contracts": 10,
      "entry_price_cents": 50,
      "cost_usd": 5.0,
      "signal": "BUY YES",
    })
    paper = store.fresh_start_paper(25.0)
    assert store.list_trades() == []
    assert store.open_positions("EV1") == []
    assert paper["paper_bankroll_usd"] == 25.0
    assert paper["paper_total_invested_usd"] == 25.0
    assert paper["paper_refill_count"] == 0


def test_paper_refills_when_bankroll_exhausted():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    max_cap = 25.0
    store.save_settings(HourlyBotSettings(enabled=True, max_spend_per_hour_usd=max_cap, mode="paper"))
    store.log_trade({
      "event_ticker": "EV1",
      "action": "exit",
      "mode": "paper",
      "status": "filled",
      "pnl_usd": -25.0,
    })
    bot = HourlyBot(store, asset="btc")
    from tests.test_hourly_bot_continuous import _live_tab

    actions = bot.run_continuous_cycle(
      _live_tab(event="EV1"),
      cfg={"hourly": {"regime": {"min_edge": 0.05, "min_expected_move_pct": 0.12}}},
    )
    assert any(a.get("action") == "paper_refill" for a in actions)
    assert not store.get_settings().auto_stopped
    paper = store.get_paper_state_dict(max_cap)
    assert paper["paper_bankroll_usd"] == 25.0
    assert paper["paper_refill_count"] == 1
    assert paper["paper_total_invested_usd"] == 50.0


def test_paper_auto_stop_when_refill_disabled():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    max_cap = 25.0
    store.save_settings(HourlyBotSettings(
      enabled=True,
      max_spend_per_hour_usd=max_cap,
      mode="paper",
      paper_auto_refill=False,
    ))
    store.log_trade({
      "event_ticker": "EV1",
      "action": "exit",
      "mode": "paper",
      "status": "filled",
      "pnl_usd": -25.0,
    })
    bot = HourlyBot(store, asset="btc")
    from tests.test_hourly_bot_continuous import _live_tab

    actions = bot.run_continuous_cycle(
      _live_tab(event="EV1"),
      cfg={"hourly": {"regime": {"min_edge": 0.05, "min_expected_move_pct": 0.12}}},
    )
    assert any(a.get("action") == "auto_stop" for a in actions)
    assert store.get_settings().auto_stopped
