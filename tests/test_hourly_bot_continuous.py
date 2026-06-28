"""Tests for continuous hourly auto-bet bot."""

from __future__ import annotations

import tempfile
from pathlib import Path

from src.trading.hourly_bot import HourlyBot, bet_qualifies
from src.trading.hourly_bot_store import HourlyBotSettings, HourlyBotStore


def _strong_bet():
  return {
    "actionable_bet": True,
    "actionable_tone": "strong",
    "actionable_headline": "STRONG ACTIONABLE BET",
  }


def _live_tab(event="KXTEST-1H", pick=None, regime_allow=True):
  pick = pick or {
    "ticker": "KXTEST-T1",
    "signal": "BUY YES",
    "label": "$2,500+",
    "kalshi_mid": 0.40,
    "edge": 0.12,
    "model_prob": 0.65,
  }
  return {
    "ok": True,
    "event": {"event_ticker": event},
    "live": {
      "primary_pick": pick,
      "current_price": 2500.0,
      "terminal_mu": 2510.0,
      "regime": {"allow_trade": regime_allow, "reasons": []},
      "strategy_threshold": {"best_edge": pick, "most_likely": pick, "contracts": [pick]},
      "strategy_range": {"best_edge": None, "most_likely": None, "contracts": []},
    },
    "locked": {
      "reference_price": 2495.0,
      "terminal_mu": 2505.0,
      "primary_pick": pick,
    },
    "brti_live": 2500.0,
  }


def test_exposure_budget_frees_on_exit():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.save_settings(HourlyBotSettings(enabled=True, max_spend_per_hour_usd=10.0))
    store.open_position({
      "id": "p1",
      "event_ticker": "EV1",
      "market_ticker": "T1",
      "side": "yes",
      "contracts": 20,
      "entry_price_cents": 40,
      "cost_usd": 8.0,
      "signal": "BUY YES",
    })
    assert store.open_exposure_usd("EV1") == 8.0
    assert store.remaining_budget_usd("EV1", 10.0) == 2.0
    store.close_position("p1")
    assert store.open_exposure_usd("EV1") == 0.0
    assert store.remaining_budget_usd("EV1", 10.0) == 10.0


def test_continuous_enter_on_strong_live_signal():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.save_settings(HourlyBotSettings(enabled=True, max_spend_per_hour_usd=10.0))
    bot = HourlyBot(store, asset="btc")
    tab = _live_tab()
    actions = bot.run_continuous_cycle(tab, cfg={"hourly": {"regime": {"min_edge": 0.05, "min_expected_move_pct": 0.12}}})
    assert len(actions) == 1
    assert actions[0]["action"] == "enter"
    assert actions[0]["entry_price_cents"] == 40
    assert store.open_exposure_usd("KXTEST-1H") > 0


def test_no_enter_when_regime_weak_and_not_actionable():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.save_settings(HourlyBotSettings(enabled=True))
    bot = HourlyBot(store, asset="btc")
    tab = _live_tab(regime_allow=False)
    tab["live"]["primary_pick"]["signal"] = "BUY YES"
    tab["live"]["regime"] = {"allow_trade": False, "reasons": ["compressed"]}
    actions = bot.run_continuous_cycle(tab, cfg={"hourly": {"regime": {}, "intrahour": {"enabled": False}}})
    assert actions == []
    assert store.open_exposure_usd("KXTEST-1H") == 0.0


def test_bet_qualifies_respects_toggles():
  pick = {"signal": "BUY YES", "ticker": "T1", "kalshi_mid": 0.4, "edge": 0.02}
  weak_bet = {"actionable_bet": False, "actionable_tone": "weak"}
  assert bet_qualifies(pick, _strong_bet(), HourlyBotSettings(enabled=True, allow_strong=True, allow_actionable=False))
  assert bet_qualifies(pick, weak_bet, HourlyBotSettings(enabled=True, allow_strong=False, allow_actionable=False))


def test_free_mode_enters_without_actionable_assessment():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.save_settings(HourlyBotSettings(
      enabled=True, max_spend_per_hour_usd=10.0, allow_strong=False, allow_actionable=False,
    ))
    bot = HourlyBot(store, asset="btc")
    tab = _live_tab(regime_allow=False)
    tab["live"]["regime"] = {"allow_trade": False, "reasons": ["compressed"]}
    actions = bot.run_continuous_cycle(tab, cfg={"hourly": {"regime": {}, "intrahour": {"enabled": False}}})
    assert len(actions) == 1
    assert actions[0]["action"] == "enter"


def test_trade_log_entry_and_exit_prices():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    pid = "pos-1"
    store.log_trade({
      "event_ticker": "EV1",
      "action": "enter",
      "mode": "paper",
      "market_ticker": "T1",
      "side": "yes",
      "contracts": 10,
      "price_cents": 40,
      "entry_price_cents": 40,
      "cost_usd": 4.0,
      "status": "filled",
      "position_id": pid,
    })
    store.log_trade({
      "event_ticker": "EV1",
      "action": "exit",
      "mode": "paper",
      "market_ticker": "T1",
      "side": "yes",
      "contracts": 10,
      "price_cents": 55,
      "entry_price_cents": 40,
      "exit_price_cents": 55,
      "pnl_usd": 1.5,
      "status": "filled",
      "position_id": pid,
    })
    trades = store.list_trades(event_ticker="EV1")
    assert len(trades) == 2
    exit_row = next(t for t in trades if t["action"] == "exit")
    assert exit_row["entry_price_cents"] == 40
    assert exit_row["exit_price_cents"] == 55
    assert exit_row["realized_pnl_usd"] == 1.5


def test_hour_interval_summary_totals():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.log_trade({
      "event_ticker": "EV1",
      "action": "enter",
      "status": "filled",
      "cost_usd": 5.0,
      "entry_price_cents": 50,
      "price_cents": 50,
    })
    store.log_trade({
      "event_ticker": "EV1",
      "action": "exit",
      "status": "filled",
      "pnl_usd": 2.25,
      "entry_price_cents": 50,
      "exit_price_cents": 72,
    })
    store.open_position({
      "id": "open1",
      "event_ticker": "EV1",
      "market_ticker": "T2",
      "side": "yes",
      "contracts": 5,
      "entry_price_cents": 30,
      "cost_usd": 1.5,
    })
    summary = store.hour_interval_summary("EV1")
    assert summary["enter_count"] == 1
    assert summary["exit_count"] == 1
    assert summary["realized_pnl_usd"] == 2.25
    assert summary["open_position_count"] == 1
    assert summary["open_exposure_usd"] == 1.5
    status = store.status("EV1")
    assert status["hourly_summary"]["realized_pnl_usd"] == 2.25


def test_enter_exit_logged_with_prices_and_hour_summary():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    event = "KXTEST-1H"
    store.log_trade({
      "event_ticker": event,
      "action": "enter",
      "mode": "paper",
      "market_ticker": "T1",
      "side": "yes",
      "contracts": 10,
      "price_cents": 40,
      "entry_price_cents": 40,
      "cost_usd": 4.0,
      "status": "filled",
      "detail": "Paper ENTER",
    })
    store.log_trade({
      "event_ticker": event,
      "action": "exit",
      "mode": "paper",
      "market_ticker": "T1",
      "side": "yes",
      "contracts": 10,
      "price_cents": 55,
      "entry_price_cents": 40,
      "exit_price_cents": 55,
      "pnl_usd": 1.5,
      "status": "filled",
      "detail": "Paper EXIT",
    })
    trades = store.list_trades(event_ticker=event)
    assert trades[0]["action"] == "exit"
    assert trades[0]["entry_price_cents"] == 40
    assert trades[0]["exit_price_cents"] == 55
    assert trades[0]["pnl_usd"] == 1.5
    assert trades[1]["action"] == "enter"
    assert trades[1]["entry_price_cents"] == 40
    summary = store.hour_interval_summary(event)
    assert summary["realized_pnl_usd"] == 1.5
    assert summary["enter_count"] == 1
    assert summary["exit_count"] == 1
    assert summary["total_entered_usd"] == 4.0
