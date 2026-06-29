"""Tests for continuous 15-minute auto-bet bot."""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.trading.slot15_bot import Slot15Bot, bet_qualifies
from src.trading.slot15_bot_store import Slot15BotSettings, Slot15BotStore


def _opened_at_seconds_ago(seconds: float) -> str:
  return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


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
      "yes_bid": 0.55,
      "yes_ask": 0.55,
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
    store.save_settings(Slot15BotSettings(use_accumulated_profit=True))
    store.log_trade({
      "event_ticker": "SLOT1",
      "action": "exit",
      "mode": "paper",
      "status": "filled",
      "pnl_usd": 5.0,
    })
    assert store.slot_bankroll_usd("SLOT1", 25.0) == 30.0
    assert store.remaining_budget_usd("SLOT1", 25.0) == 25.0


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
    tab["kalshi"]["yes_bid"] = 0.55
    tab["kalshi"]["yes_ask"] = 0.55
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
    tab["kalshi"]["yes_bid"] = 0.60
    tab["kalshi"]["yes_ask"] = 0.60
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


def test_auto_stop_when_slot_bankroll_exhausted():
  with tempfile.TemporaryDirectory() as tmp:
    store = Slot15BotStore(Path(tmp) / "bot.db")
    max_cap = 25.0
    store.save_settings(Slot15BotSettings(enabled=True, max_spend_per_slot_usd=max_cap, mode="live"))
    store.log_trade({
      "event_ticker": "SLOT1",
      "action": "exit",
      "status": "filled",
      "pnl_usd": -25.0,
    })
    assert store.remaining_budget_usd("SLOT1", max_cap) == 0.0
    bot = Slot15Bot(store, asset="btc")
    tab = _live_tab(slot_key="SLOT1")
    actions = bot.run_continuous_cycle(tab)
    assert any(a.get("action") == "auto_stop" for a in actions)
    assert store.get_settings().enabled
    assert store.get_settings().auto_stopped
    st = store.status("SLOT1")
    assert st["auto_stopped"] is True
    assert st["last_skip_reason"] == "auto_stopped_budget_exhausted"


def test_no_entries_after_slot_auto_stop():
  with tempfile.TemporaryDirectory() as tmp:
    store = Slot15BotStore(Path(tmp) / "bot.db")
    store.save_settings(Slot15BotSettings(
      enabled=True, auto_stopped=True, max_spend_per_slot_usd=25.0,
    ))
    bot = Slot15Bot(store, asset="btc")
    assert bot.run_continuous_cycle(_live_tab(slot_key="SLOT1")) == []
    assert store.last_skip_reason() == "auto_stopped_budget_exhausted"


def test_manual_reenable_after_slot_auto_stop():
  with tempfile.TemporaryDirectory() as tmp:
    store = Slot15BotStore(Path(tmp) / "bot.db")
    store.save_settings(Slot15BotSettings(
      enabled=True, auto_stopped=True, max_spend_per_slot_usd=25.0,
    ))
    store.log_trade({
      "event_ticker": "SLOT1",
      "action": "exit",
      "status": "filled",
      "pnl_usd": -25.0,
    })
    store.save_settings(Slot15BotSettings(
      enabled=True, auto_stopped=False, max_spend_per_slot_usd=50.0,
    ))
    store.reset_paper_bankroll(50.0)
    bot = Slot15Bot(store, asset="btc")
    actions = bot.run_continuous_cycle(_live_tab(slot_key="SLOT1"))
    assert any(a.get("action") == "enter" for a in actions)
    assert store.get_settings().enabled


def test_profit_target_exit_on_hold_monitor():
  with tempfile.TemporaryDirectory() as tmp:
    store = Slot15BotStore(Path(tmp) / "bot.db")
    slot_key = "2025-06-28T14:00:00-04:00"
    store.save_settings(Slot15BotSettings(
      enabled=True, max_spend_per_slot_usd=25.0, take_profit_pct=0.25, min_hold_seconds=0,
    ))
    store.open_position({
      "id": "p1",
      "event_ticker": slot_key,
      "market_ticker": "KXBTC15M-TEST",
      "side": "yes",
      "contracts": 25,
      "entry_price_cents": 40,
      "cost_usd": 10.0,
      "signal": "LONG",
      "opened_at": _opened_at_seconds_ago(60),
    })
    bot = Slot15Bot(store, asset="btc")
    tab = _live_tab(slot_key=slot_key)
    tab["monitor"]["action"] = "HOLD"
    tab["kalshi"]["yes_mid"] = 0.52
    tab["kalshi"]["yes_bid"] = 0.52
    tab["kalshi"]["yes_ask"] = 0.52
    actions = bot.run_continuous_cycle(tab)
    exits = [a for a in actions if a.get("action") == "exit"]
    assert len(exits) == 1
    assert "PROFIT TARGET" in exits[0]["detail"] or "LEG TAKE PROFIT" in exits[0]["detail"]
    assert exits[0]["pnl_usd"] == 3.0


def test_profit_target_increases_slot_bankroll():
  with tempfile.TemporaryDirectory() as tmp:
    store = Slot15BotStore(Path(tmp) / "bot.db")
    slot_key = "2025-06-28T14:00:00-04:00"
    store.save_settings(Slot15BotSettings(
      enabled=True, max_spend_per_slot_usd=25.0, take_profit_pct=0.25, min_hold_seconds=0,
      use_accumulated_profit=True,
    ))
    store.open_position({
      "id": "p1",
      "event_ticker": slot_key,
      "market_ticker": "KXBTC15M-TEST",
      "side": "yes",
      "contracts": 25,
      "entry_price_cents": 40,
      "cost_usd": 10.0,
      "signal": "LONG",
      "opened_at": _opened_at_seconds_ago(60),
    })
    bot = Slot15Bot(store, asset="btc")
    tab = _live_tab(slot_key=slot_key)
    tab["monitor"]["action"] = "HOLD"
    tab["kalshi"]["yes_mid"] = 0.52
    tab["kalshi"]["yes_bid"] = 0.52
    tab["kalshi"]["yes_ask"] = 0.52
    bot.run_continuous_cycle(tab)
    assert store.realized_pnl_usd(slot_key) == 3.0
    settings = store.get_settings()
    assert store.slot_bankroll_usd(slot_key, 25.0, settings) == 28.0


def test_no_exit_when_profit_below_threshold_slot15():
  with tempfile.TemporaryDirectory() as tmp:
    store = Slot15BotStore(Path(tmp) / "bot.db")
    slot_key = "2025-06-28T14:00:00-04:00"
    store.save_settings(Slot15BotSettings(
      enabled=True, max_spend_per_slot_usd=25.0, take_profit_pct=0.25, min_hold_seconds=0,
    ))
    store.open_position({
      "id": "p1",
      "event_ticker": slot_key,
      "market_ticker": "KXBTC15M-TEST",
      "side": "yes",
      "contracts": 25,
      "entry_price_cents": 40,
      "cost_usd": 10.0,
      "signal": "LONG",
      "opened_at": _opened_at_seconds_ago(60),
    })
    bot = Slot15Bot(store, asset="btc")
    tab = _live_tab(slot_key=slot_key)
    tab["monitor"]["action"] = "HOLD"
    tab["kalshi"]["yes_mid"] = 0.41
    tab["kalshi"]["yes_bid"] = 0.41
    tab["kalshi"]["yes_ask"] = 0.41
    cfg = {
      "intra_slot": {
        "bot": {
          "leg_take_profit_cents": 5,
          "leg_take_profit_usd": 5.0,
        },
      },
    }
    actions = bot.run_continuous_cycle(tab, cfg=cfg)
    assert not any(a.get("action") == "exit" for a in actions)
    assert len(store.open_positions(slot_key)) == 1


def test_profit_trail_exit_slot15():
  with tempfile.TemporaryDirectory() as tmp:
    store = Slot15BotStore(Path(tmp) / "bot.db")
    slot_key = "2025-06-28T14:00:00-04:00"
    store.save_settings(Slot15BotSettings(
      enabled=True,
      max_spend_per_slot_usd=25.0,
      take_profit_mode="hybrid",
      trail_giveback_pct=0.40,
      min_hold_seconds=0,
    ))
    store.open_position({
      "id": "p1",
      "event_ticker": slot_key,
      "market_ticker": "KXBTC15M-TEST",
      "side": "yes",
      "contracts": 25,
      "entry_price_cents": 40,
      "cost_usd": 10.0,
      "signal": "LONG",
      "opened_at": _opened_at_seconds_ago(60),
    })
    store.update_position_peaks("p1", 5.0, 10.0)
    bot = Slot15Bot(store, asset="btc")
    tab = _live_tab(slot_key=slot_key)
    tab["monitor"]["action"] = "HOLD"
    tab["kalshi"]["yes_mid"] = 0.50
    tab["kalshi"]["yes_bid"] = 0.50
    tab["kalshi"]["yes_ask"] = 0.50
    cfg = {
      "intra_slot": {
        "bot": {
          "leg_take_profit_cents": 99,
          "leg_take_profit_usd": 99.0,
        },
      },
    }
    actions = bot.run_continuous_cycle(tab, cfg=cfg)
    exits = [a for a in actions if a.get("action") == "exit"]
    assert len(exits) == 1
    assert "LEG TRAIL" in exits[0]["detail"] or "PROFIT TRAIL" in exits[0]["detail"]


def test_wide_spread_enters_with_tab_max_spread():
  with tempfile.TemporaryDirectory() as tmp:
    store = Slot15BotStore(Path(tmp) / "bot.db")
    store.save_settings(Slot15BotSettings(enabled=True, max_spend_per_slot_usd=10.0))
    bot = Slot15Bot(store, asset="btc")
    tab = _live_tab()
    tab["kalshi"]["yes_bid"] = 0.01
    tab["kalshi"]["yes_ask"] = 0.40
    tab["paper_max_spread_cents"] = 40
    actions = bot.run_continuous_cycle(tab)
    assert len(actions) == 1
    assert actions[0]["action"] == "enter"
    assert actions[0]["entry_price_cents"] == 40


def test_wide_spread_skipped_with_default_paper_max():
  with tempfile.TemporaryDirectory() as tmp:
    store = Slot15BotStore(Path(tmp) / "bot.db")
    store.save_settings(Slot15BotSettings(enabled=True, max_spend_per_slot_usd=10.0))
    bot = Slot15Bot(store, asset="btc")
    tab = _live_tab()
    tab["kalshi"]["yes_bid"] = 0.01
    tab["kalshi"]["yes_ask"] = 0.40
    tab["paper_max_spread_cents"] = 15
    actions = bot.run_continuous_cycle(tab)
    assert actions == []
    attempt = store.last_entry_attempt()
    assert attempt is not None
    assert attempt["skip_reason"] == "spread_too_wide"
    assert attempt["bid_cents"] == 1
    assert attempt["ask_cents"] == 40
    assert attempt["spread_cents"] == 39
    assert store.last_skip_reason() == "spread_too_wide"


def test_probe_long_during_no_trade_window():
  with tempfile.TemporaryDirectory() as tmp:
    store = Slot15BotStore(Path(tmp) / "bot.db")
    store.save_settings(Slot15BotSettings(enabled=True, max_spend_per_slot_usd=10.0))
    bot = Slot15Bot(store, asset="btc")
    tab = _live_tab()
    tab["prediction"]["signal"] = "NO TRADE"
    tab["prediction"]["prob_up"] = 0.62
    tab["monitor"]["elapsed_pct"] = 10.0
    tab["probe_no_trade"] = {"enabled": True, "min_prob": 0.58, "min_elapsed_pct": 7.0}
    tab["kalshi"]["yes_bid"] = 0.48
    tab["kalshi"]["yes_ask"] = 0.50
    tab["kalshi"]["yes_mid"] = 0.49
    actions = bot.run_continuous_cycle(tab)
    assert len(actions) == 1
    assert actions[0]["action"] == "enter"
    assert actions[0]["signal"] == "LONG"


def test_slot_times_match_rejects_stale_prediction_slot():
  import pandas as pd

  from src.features.slots import slot_times_match

  tz = "America/New_York"
  stale_slot = pd.Timestamp("2025-06-28T13:45:00", tz=tz)
  current_slot = "2025-06-28T14:00:00-04:00"
  assert not slot_times_match(stale_slot, current_slot, tz)
  assert slot_times_match(
    pd.Timestamp("2025-06-28T14:00:00", tz=tz),
    current_slot,
    tz,
  )


def test_slot_prediction_refresh_needed_when_slots_differ():
  import pandas as pd

  from src.features.slots import current_slot_start, slot_times_match

  tz = "America/New_York"
  slot_s = current_slot_start(tz_name=tz)
  stale_slot = slot_s - pd.Timedelta(minutes=15)
  assert not slot_times_match(stale_slot, slot_s.isoformat(), tz)
  assert slot_times_match(slot_s, slot_s.isoformat(), tz)


def test_leg_take_profit_on_small_mark_gain():
  with tempfile.TemporaryDirectory() as tmp:
    store = Slot15BotStore(Path(tmp) / "bot.db")
    slot_key = "2025-06-28T14:00:00-04:00"
    store.save_settings(Slot15BotSettings(enabled=True, max_spend_per_slot_usd=25.0, min_hold_seconds=0))
    store.open_position({
      "id": "p1",
      "event_ticker": slot_key,
      "market_ticker": "KXBTC15M-TEST",
      "side": "yes",
      "contracts": 10,
      "entry_price_cents": 55,
      "cost_usd": 5.5,
      "signal": "LONG",
      "opened_at": _opened_at_seconds_ago(30),
    })
    bot = Slot15Bot(store, asset="btc")
    tab = _live_tab(slot_key=slot_key)
    tab["monitor"]["action"] = "HOLD"
    tab["kalshi"]["yes_mid"] = 0.58
    tab["kalshi"]["yes_bid"] = 0.58
    tab["kalshi"]["yes_ask"] = 0.58
    actions = bot.run_continuous_cycle(tab)
    exits = [a for a in actions if a.get("action") == "exit"]
    assert len(exits) == 1
    assert "LEG TAKE PROFIT" in exits[0]["detail"]


def test_leg_stop_on_mark_drawdown():
  with tempfile.TemporaryDirectory() as tmp:
    store = Slot15BotStore(Path(tmp) / "bot.db")
    slot_key = "2025-06-28T14:00:00-04:00"
    store.save_settings(Slot15BotSettings(enabled=True, max_spend_per_slot_usd=25.0))
    store.open_position({
      "id": "p1",
      "event_ticker": slot_key,
      "market_ticker": "KXBTC15M-TEST",
      "side": "yes",
      "contracts": 10,
      "entry_price_cents": 55,
      "cost_usd": 5.5,
      "signal": "LONG",
    })
    bot = Slot15Bot(store, asset="btc")
    tab = _live_tab(slot_key=slot_key)
    tab["monitor"]["action"] = "HOLD"
    tab["monitor"]["seconds_remaining"] = 120.0
    tab["kalshi"]["yes_mid"] = 0.46
    tab["kalshi"]["yes_bid"] = 0.46
    tab["kalshi"]["yes_ask"] = 0.46
    actions = bot.run_continuous_cycle(tab)
    exits = [a for a in actions if a.get("action") == "exit"]
    assert len(exits) == 1
    assert "LEG STOP" in exits[0]["detail"]


def test_reassess_neutral_take_profit_while_green():
  with tempfile.TemporaryDirectory() as tmp:
    store = Slot15BotStore(Path(tmp) / "bot.db")
    slot_key = "2025-06-28T14:00:00-04:00"
    store.save_settings(Slot15BotSettings(
      enabled=True,
      max_spend_per_slot_usd=25.0,
      take_profit_pct=0.50,
      min_hold_seconds=0,
    ))
    store.open_position({
      "id": "p1",
      "event_ticker": slot_key,
      "market_ticker": "KXBTC15M-TEST",
      "side": "yes",
      "contracts": 20,
      "entry_price_cents": 50,
      "cost_usd": 10.0,
      "signal": "LONG",
      "opened_at": _opened_at_seconds_ago(120),
    })
    bot = Slot15Bot(store, asset="btc")
    tab = _live_tab(slot_key=slot_key)
    tab["monitor"]["action"] = "HOLD"
    tab["monitor"]["reassessed_prob_up"] = 0.51
    tab["monitor"]["reassess_summary"] = "Reassessed: 51% UP / 49% DOWN at close"
    tab["kalshi"]["yes_mid"] = 0.52
    tab["kalshi"]["yes_bid"] = 0.52
    tab["kalshi"]["yes_ask"] = 0.52
    cfg = {
      "intra_slot": {
        "bot": {
          "leg_take_profit_cents": 10,
          "leg_take_profit_usd": 99.0,
          "reassess_neutral_take_profit": True,
          "reassess_neutral_band": 0.07,
          "reassess_neutral_min_unrealized_usd": 0.05,
        },
      },
    }
    actions = bot.run_continuous_cycle(tab, cfg=cfg)
    exits = [a for a in actions if a.get("action") == "exit"]
    assert len(exits) == 1
    assert "REASSESS NEUTRAL TP" in exits[0]["detail"]


def test_enrich_open_positions_leg_alert_not_slot_only():
  from src.trading.slot15_bot import enrich_open_positions_live

  positions = [{
    "id": "p1",
    "side": "yes",
    "entry_price_cents": 55,
    "contracts": 10,
    "cost_usd": 5.5,
    "signal": "LONG",
  }]
  tab = _live_tab()
  tab["monitor"]["action"] = "HOLD"
  tab["monitor"]["message"] = "Hold — still winning with room to run."
  tab["kalshi"]["yes_mid"] = 0.58
  tab["kalshi"]["yes_bid"] = 0.58
  tab["kalshi"]["yes_ask"] = 0.58
  enriched = enrich_open_positions_live(positions, tab)
  alert = enriched[0]["position_alert"]
  assert alert["alert"] == "TAKE PROFIT"
  assert alert.get("slot_monitor_alert") == "HOLD"
  assert alert.get("mark_vs_entry_cents") == 3


def test_scale_in_second_leg_same_cycle():
  cfg = {
    "intra_slot": {
      "bot": {
        "leg_take_profit_cents": 99,
        "leg_take_profit_usd": 99.0,
        "entry_strategy": {
          "enabled": True,
          "max_entries_per_cycle": 2,
          "max_concurrent_positions": 3,
          "kelly_enabled": False,
          "allow_scale_in": True,
          "scale_in_max_legs_per_ticker": 3,
          "scale_in_min_unrealized_pnl_usd": 0.05,
          "min_ask_edge_cents": 0,
        }
      }
    }
  }
  with tempfile.TemporaryDirectory() as tmp:
    store = Slot15BotStore(Path(tmp) / "bot.db")
    slot_key = "2025-06-28T14:00:00-04:00"
    store.save_settings(Slot15BotSettings(
      enabled=True, max_spend_per_slot_usd=100.0, aggressive_entries=True,
    ))
    store.open_position({
      "id": "p1",
      "event_ticker": slot_key,
      "market_ticker": "KXBTC15M-TEST",
      "side": "yes",
      "contracts": 20,
      "entry_price_cents": 50,
      "cost_usd": 10.0,
      "signal": "LONG",
    })
    bot = Slot15Bot(store, asset="btc")
    tab = _live_tab(slot_key=slot_key)
    tab["kalshi"]["yes_mid"] = 0.52
    tab["kalshi"]["yes_bid"] = 0.52
    tab["kalshi"]["yes_ask"] = 0.52
    actions = bot.run_continuous_cycle(tab, cfg=cfg)
    enters = [a for a in actions if a.get("action") == "enter"]
    assert len(enters) == 1
    assert len(store.open_positions(slot_key)) == 2
