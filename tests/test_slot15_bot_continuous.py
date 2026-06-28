"""Tests for continuous 15-minute auto-bet bot."""

from __future__ import annotations

import tempfile
from pathlib import Path

from src.trading.slot15_bot import Slot15Bot, bet_qualifies
from src.trading.slot15_bot_store import Slot15BotSettings, Slot15BotStore


def _strong_bet():
  return {
    "actionable_bet": True,
    "actionable_tone": "strong",
    "actionable_headline": "STRONG ACTIONABLE BET",
  }


def _live_tab(slot_key="2025-06-28T14:00:00-04:00", signal="LONG"):
  return {
    "ok": True,
    "slot_key": slot_key,
    "slot_label": "2:00–2:15 PM ET",
    "prediction": {
      "signal": signal,
      "model_signal": signal,
      "prob_up": 0.65,
      "regime_notes": [],
      "reference_price": 100000.0,
      "expected_move": 80.0,
    },
    "monitor": {
      "slot_start": slot_key,
      "signal_at_open": signal,
      "action": "HOLD",
      "reference_price": 100000.0,
      "late_entry_action": "",
      "flip_action": "",
    },
    "kalshi": {
      "market_ticker": "KXBTC15M-TEST",
      "yes_mid": 0.55,
    },
    "bet_assessment": _strong_bet(),
  }


def test_remaining_budget_accounts_for_realized_losses():
  with tempfile.TemporaryDirectory() as tmp:
    store = Slot15BotStore(Path(tmp) / "bot.db")
    store.log_trade({
      "event_ticker": "SLOT1",
      "action": "exit",
      "status": "filled",
      "pnl_usd": -3.0,
    })
    assert store.remaining_budget_usd("SLOT1", 25.0) == 22.0
    assert store.slot_bankroll_usd("SLOT1", 25.0) == 22.0


def test_remaining_budget_increases_after_win():
  with tempfile.TemporaryDirectory() as tmp:
    store = Slot15BotStore(Path(tmp) / "bot.db")
    store.log_trade({
      "event_ticker": "SLOT1",
      "action": "exit",
      "status": "filled",
      "pnl_usd": 5.0,
    })
    assert store.remaining_budget_usd("SLOT1", 25.0) == 30.0


def test_no_exit_on_cut_loss_when_flat_pnl():
  with tempfile.TemporaryDirectory() as tmp:
    store = Slot15BotStore(Path(tmp) / "bot.db")
    store.save_settings(Slot15BotSettings(enabled=True, max_spend_per_slot_usd=25.0))
    store.open_position({
      "id": "p1",
      "event_ticker": "2025-06-28T14:00:00-04:00",
      "market_ticker": "KXBTC15M-TEST",
      "side": "yes",
      "contracts": 30,
      "entry_price_cents": 55,
      "cost_usd": 16.5,
      "signal": "LONG",
    })
    bot = Slot15Bot(store, asset="btc")
    tab = _live_tab()
    tab["monitor"]["action"] = "CUT LOSS"
    tab["monitor"]["message"] = "Signal weakened — cut loss"
    tab["kalshi"]["yes_mid"] = 0.55
    actions = bot.run_continuous_cycle(tab)
    assert actions == []
    assert len(store.open_positions(tab["slot_key"])) == 1


def test_reentry_cooldown_blocks_immediate_reentry():
  with tempfile.TemporaryDirectory() as tmp:
    store = Slot15BotStore(Path(tmp) / "bot.db")
    store.save_settings(Slot15BotSettings(
      enabled=True, max_spend_per_slot_usd=25.0,
      allow_strong=False, allow_actionable=False,
      reentry_cooldown_seconds=120,
    ))
    store.record_exit_cooldown("2025-06-28T14:00:00-04:00", "KXBTC15M-TEST")
    bot = Slot15Bot(store, asset="btc")
    tab = _live_tab()
    actions = bot.run_continuous_cycle(tab)
    assert actions == []


def test_exposure_budget_frees_on_exit():
  with tempfile.TemporaryDirectory() as tmp:
    store = Slot15BotStore(Path(tmp) / "bot.db")
    store.save_settings(Slot15BotSettings(enabled=True, max_spend_per_slot_usd=10.0))
    store.open_position({
      "id": "p1",
      "event_ticker": "SLOT1",
      "market_ticker": "T1",
      "side": "yes",
      "contracts": 20,
      "entry_price_cents": 40,
      "cost_usd": 8.0,
      "signal": "LONG",
    })
    assert store.open_exposure_usd("SLOT1") == 8.0
    assert store.remaining_budget_usd("SLOT1", 10.0) == 2.0
    store.close_position("p1")
    assert store.open_exposure_usd("SLOT1") == 0.0
    assert store.remaining_budget_usd("SLOT1", 10.0) == 10.0


def test_continuous_enter_on_strong_long_signal():
  with tempfile.TemporaryDirectory() as tmp:
    store = Slot15BotStore(Path(tmp) / "bot.db")
    store.save_settings(Slot15BotSettings(enabled=True, max_spend_per_slot_usd=10.0))
    bot = Slot15Bot(store, asset="btc")
    tab = _live_tab()
    actions = bot.run_continuous_cycle(tab)
    assert len(actions) == 1
    assert actions[0]["action"] == "enter"
    assert actions[0]["entry_price_cents"] == 55
    assert store.open_exposure_usd(tab["slot_key"]) > 0


def test_continuous_exit_on_take_profit():
  with tempfile.TemporaryDirectory() as tmp:
    store = Slot15BotStore(Path(tmp) / "bot.db")
    store.save_settings(Slot15BotSettings(enabled=True, max_spend_per_slot_usd=10.0))
    slot_key = "2025-06-28T14:00:00-04:00"
    store.open_position({
      "id": "p1",
      "event_ticker": slot_key,
      "market_ticker": "KXBTC15M-TEST",
      "side": "yes",
      "contracts": 10,
      "entry_price_cents": 40,
      "cost_usd": 4.0,
      "signal": "LONG",
    })
    bot = Slot15Bot(store, asset="btc")
    tab = _live_tab(slot_key=slot_key)
    tab["monitor"]["action"] = "TAKE PROFIT"
    tab["prediction"]["signal"] = "NO TRADE"
    tab["bet_assessment"] = {"actionable_bet": False, "actionable_tone": "weak"}
    tab["kalshi"]["yes_mid"] = 0.60
    actions = bot.run_continuous_cycle(tab)
    exits = [a for a in actions if a.get("action") == "exit"]
    assert len(exits) == 1
    assert exits[0]["exit_price_cents"] == 60
    assert store.open_exposure_usd(slot_key) == 0.0


def test_bet_qualifies_respects_toggles():
  weak_bet = {"actionable_bet": False, "actionable_tone": "weak"}
  assert bet_qualifies("LONG", _strong_bet(), Slot15BotSettings(enabled=True, allow_strong=True, allow_actionable=False))
  assert bet_qualifies("LONG", weak_bet, Slot15BotSettings(enabled=True, allow_strong=False, allow_actionable=False))


def test_free_mode_enters_without_actionable_assessment():
  with tempfile.TemporaryDirectory() as tmp:
    store = Slot15BotStore(Path(tmp) / "bot.db")
    store.save_settings(Slot15BotSettings(
      enabled=True, max_spend_per_slot_usd=10.0, allow_strong=False, allow_actionable=False,
    ))
    bot = Slot15Bot(store, asset="btc")
    tab = _live_tab()
    tab["bet_assessment"] = {"actionable_bet": False, "actionable_tone": "weak"}
    actions = bot.run_continuous_cycle(tab)
    assert len(actions) == 1
    assert actions[0]["action"] == "enter"


def test_late_entry_signal_enters():
  with tempfile.TemporaryDirectory() as tmp:
    store = Slot15BotStore(Path(tmp) / "bot.db")
    store.save_settings(Slot15BotSettings(enabled=True, max_spend_per_slot_usd=10.0))
    bot = Slot15Bot(store, asset="btc")
    tab = _live_tab(signal="NO TRADE")
    tab["prediction"]["signal"] = "NO TRADE"
    tab["monitor"]["late_entry_action"] = "LATE LONG"
    tab["bet_assessment"] = _strong_bet()
    actions = bot.run_continuous_cycle(tab)
    assert len(actions) == 1
    assert actions[0]["side"] == "yes"


def test_slot_interval_summary_totals():
  with tempfile.TemporaryDirectory() as tmp:
    store = Slot15BotStore(Path(tmp) / "bot.db")
    store.log_trade({
      "event_ticker": "SLOT1",
      "action": "enter",
      "status": "filled",
      "cost_usd": 5.0,
      "entry_price_cents": 50,
      "price_cents": 50,
    })
    store.log_trade({
      "event_ticker": "SLOT1",
      "action": "exit",
      "status": "filled",
      "pnl_usd": 2.25,
      "entry_price_cents": 50,
      "exit_price_cents": 72,
    })
    summary = store.slot_interval_summary("SLOT1")
    assert summary["enter_count"] == 1
    assert summary["exit_count"] == 1
    assert summary["realized_pnl_usd"] == 2.25


def test_entries_never_exceed_max_at_risk():
  with tempfile.TemporaryDirectory() as tmp:
    store = Slot15BotStore(Path(tmp) / "bot.db")
    max_cap = 10.0
    store.save_settings(Slot15BotSettings(enabled=True, max_spend_per_slot_usd=max_cap))
    bot = Slot15Bot(store, asset="btc")
    tab = _live_tab(slot_key="SLOT1")

    for _ in range(5):
      actions = bot.run_continuous_cycle(tab)
      exposure = store.open_exposure_usd("SLOT1")
      assert exposure <= max_cap + 0.01
      for pos in list(store.open_positions("SLOT1")):
        store.close_position(pos["id"])
        store.log_trade({
          "event_ticker": "SLOT1",
          "action": "exit",
          "mode": "paper",
          "market_ticker": pos["market_ticker"],
          "side": pos["side"],
          "contracts": pos["contracts"],
          "price_cents": pos["entry_price_cents"],
          "entry_price_cents": pos["entry_price_cents"],
          "exit_price_cents": pos["entry_price_cents"],
          "pnl_usd": 0.0,
          "status": "filled",
          "position_id": pos["id"],
        })

    assert store.open_exposure_usd("SLOT1") == 0.0
