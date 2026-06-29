"""Tests for continuous hourly auto-bet bot."""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.trading.hourly_bot import HourlyBot, bet_qualifies
from src.trading.hourly_bot_store import HourlyBotSettings, HourlyBotStore


def _opened_at_seconds_ago(seconds: float) -> str:
  return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


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
    "yes_bid": 0.40,
    "yes_ask": 0.40,
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


def test_paper_enter_logs_bid_ask_spread():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.save_settings(HourlyBotSettings(
      enabled=True, max_spend_per_hour_usd=10.0,
      allow_strong=False, allow_actionable=False,
    ))
    bot = HourlyBot(store, asset="btc")
    tab = _live_tab()
    tab["live"]["primary_pick"]["yes_bid"] = 0.40
    tab["live"]["primary_pick"]["yes_ask"] = 0.42
    actions = bot.run_continuous_cycle(tab)
    assert len(actions) == 1
    enter = actions[0]
    assert enter["entry_bid_cents"] == 40
    assert enter["entry_ask_cents"] == 42
    assert enter["entry_spread_cents"] == 2
    assert enter["entry_price_cents"] == 42
    assert "book:" in (enter.get("detail") or "")


def test_paper_skips_penny_ask():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.save_settings(HourlyBotSettings(
      enabled=True, max_spend_per_hour_usd=25.0,
      allow_strong=False, allow_actionable=False,
    ))
    bot = HourlyBot(store, asset="btc")
    tab = _live_tab(pick={
      "ticker": "KXETHD-T1270",
      "signal": "BUY YES",
      "label": "$1,270 or above",
      "kalshi_mid": 0.01,
      "yes_bid": 0.01,
      "yes_ask": 0.01,
      "edge": 0.10,
      "model_prob": 0.99,
    })
    actions = bot.run_continuous_cycle(tab)
    assert actions == []
    assert store.last_skip_reason() == "price_floor"


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
    store.save_settings(HourlyBotSettings(enabled=True, allow_strong=True, allow_actionable=False))
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


def test_hour_interval_summary_backfills_missing_exit_pnl():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.log_trade({
      "event_ticker": "EV1",
      "action": "enter",
      "status": "filled",
      "cost_usd": 4.0,
      "entry_price_cents": 40,
      "price_cents": 40,
      "side": "yes",
      "contracts": 10,
    })
    store.log_trade({
      "event_ticker": "EV1",
      "action": "exit",
      "status": "filled",
      "side": "yes",
      "contracts": 10,
      "entry_price_cents": 40,
      "exit_price_cents": 55,
      "price_cents": 55,
    })
    summary = store.hour_interval_summary("EV1")
    assert summary["exit_count"] == 1
    assert summary["realized_pnl_usd"] == 1.5


def test_list_trades_without_event_filter_keeps_all_hours():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    for evt in ("KXTEST-H1", "KXTEST-H2"):
      store.log_trade({
        "event_ticker": evt,
        "action": "enter",
        "mode": "paper",
        "market_ticker": "T1",
        "side": "yes",
        "contracts": 5,
        "entry_price_cents": 40,
        "cost_usd": 2.0,
        "status": "filled",
      })
    all_trades = store.list_trades(limit=10)
    assert len(all_trades) == 2
    hour1_only = store.list_trades(limit=10, event_ticker="KXTEST-H1")
    assert len(hour1_only) == 1


def test_enrich_open_positions_live_mark_to_market():
  tab = _live_tab(pick={
    "ticker": "KXTEST-T1",
    "signal": "BUY YES",
    "label": "$2,500+",
    "kalshi_mid": 0.55,
    "yes_bid": 0.55,
    "yes_ask": 0.55,
    "edge": 0.08,
  })
  positions = [{
    "id": "p1",
    "market_ticker": "KXTEST-T1",
    "side": "yes",
    "contracts": 10,
    "entry_price_cents": 40,
    "cost_usd": 4.0,
    "signal": "BUY YES",
  }]
  from src.trading.hourly_bot import enrich_open_positions_live
  enriched = enrich_open_positions_live(positions, tab, cfg={"hourly": {"regime": {}}})
  assert len(enriched) == 1
  assert enriched[0]["mark_price_cents"] == 55
  assert enriched[0]["unrealized_pnl_usd"] == 1.5
  assert enriched[0]["position_alert"]["alert"] in ("HOLD", "TAKE PROFIT", "CUT LOSSES")


def test_enrich_open_positions_band_no_not_cut_when_spot_above():
  tab = _live_tab(pick={
    "ticker": "KXETH-BAND-LOW",
    "signal": "BUY YES",
    "label": "$1,530 to 1,549.99",
    "kalshi_mid": 0.08,
    "yes_bid": 0.08,
    "yes_ask": 0.08,
    "edge": 0.02,
    "contract_type": "range",
    "strike_type": "between",
    "floor_strike": 1530.0,
    "cap_strike": 1549.99,
  })
  tab["brti_live"] = 1565.0
  tab["live"]["current_price"] = 1565.0
  tab["live"]["regime"] = {"allow_trade": False, "reasons": ["compressed"]}
  tab["live"]["strategy_range"] = {
    "contracts": [tab["live"]["primary_pick"]],
  }
  positions = [{
    "id": "p1",
    "market_ticker": "KXETH-BAND-LOW",
    "side": "no",
    "contracts": 2,
    "entry_price_cents": 90,
    "cost_usd": 1.8,
    "signal": "BUY NO",
  }]
  from src.trading.hourly_bot import enrich_open_positions_live
  enriched = enrich_open_positions_live(positions, tab, cfg={"hourly": {"regime": {}}})
  assert enriched[0]["unrealized_pnl_usd"] == 0.04
  assert enriched[0]["position_alert"]["alert"] == "HOLD"


def test_settings_save_does_not_delete_trades():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.log_trade({
      "event_ticker": "EV1",
      "action": "enter",
      "mode": "paper",
      "status": "filled",
      "cost_usd": 1.0,
      "entry_price_cents": 50,
    })
    store.save_settings(HourlyBotSettings(enabled=False, allow_strong=False, allow_actionable=False))
    assert len(store.list_trades()) == 1


def test_remaining_budget_accounts_for_realized_losses():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.log_trade({
      "event_ticker": "EV1",
      "action": "exit",
      "status": "filled",
      "pnl_usd": -3.0,
    })
    assert store.remaining_budget_usd("EV1", 25.0) == 22.0
    assert store.hour_bankroll_usd("EV1", 25.0) == 22.0


def test_remaining_budget_increases_after_win():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.save_settings(HourlyBotSettings(use_accumulated_profit=True))
    store.log_trade({
      "event_ticker": "EV1",
      "action": "exit",
      "mode": "paper",
      "status": "filled",
      "pnl_usd": 5.0,
    })
    assert store.hour_bankroll_usd("EV1", 25.0) == 30.0
    # At-risk cap still limits concurrent deployment to max_cap.
    assert store.remaining_budget_usd("EV1", 25.0) == 25.0


def test_no_exit_on_cut_losses_when_flat_pnl():
  """Regime-block CUT LOSSES at same mark should not churn paper-exit."""
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.save_settings(HourlyBotSettings(enabled=True, max_spend_per_hour_usd=25.0))
    store.open_position({
      "id": "p1",
      "event_ticker": "KXTEST-1H",
      "market_ticker": "KXTEST-T1",
      "side": "yes",
      "contracts": 30,
      "entry_price_cents": 80,
      "cost_usd": 24.0,
      "signal": "BUY YES",
      "entry_edge": 0.12,
    })
    bot = HourlyBot(store, asset="btc")
    tab = _live_tab(regime_allow=False)
    tab["live"]["primary_pick"]["kalshi_mid"] = 0.80
    tab["live"]["primary_pick"]["yes_bid"] = 0.80
    tab["live"]["primary_pick"]["yes_ask"] = 0.80
    tab["live"]["regime"] = {"allow_trade": False, "reasons": ["compressed"]}
    actions = bot.run_continuous_cycle(tab, cfg={"hourly": {"regime": {"min_edge": 0.05}}})
    assert actions == []
    assert len(store.open_positions("KXTEST-1H")) == 1


def test_reentry_cooldown_blocks_immediate_reentry():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.save_settings(HourlyBotSettings(
      enabled=True, max_spend_per_hour_usd=25.0,
      allow_strong=False, allow_actionable=False,
      reentry_cooldown_seconds=120,
    ))
    store.record_exit_cooldown("KXTEST-1H", "KXTEST-T1")
    bot = HourlyBot(store, asset="btc")
    tab = _live_tab()
    actions = bot.run_continuous_cycle(tab, cfg={"hourly": {"regime": {}, "intrahour": {"enabled": False}}})
    assert actions == []


def test_entries_never_exceed_max_at_risk():
  """Open exposure stays within cap; cumulative enters may exceed it after round-trips."""
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    max_cap = 10.0
    store.save_settings(HourlyBotSettings(enabled=True, max_spend_per_hour_usd=max_cap))
    bot = HourlyBot(store, asset="btc")
    cfg = {
      "hourly": {
        "regime": {"min_edge": 0.05, "min_expected_move_pct": 0.12},
        "bot": {"entry_strategy": {"enabled": False}},
      }
    }
    tab = _live_tab(event="EV1")
    cumulative = 0.0

    for _ in range(5):
      actions = bot.run_continuous_cycle(tab, cfg=cfg)
      exposure = store.open_exposure_usd("EV1")
      assert exposure <= max_cap + 0.01
      for a in actions:
        if a.get("action") == "enter" and a.get("status") == "filled":
          cumulative += float(a.get("cost_usd") or 0)
          assert float(a.get("cost_usd") or 0) <= store.remaining_budget_usd("EV1", max_cap) + float(a.get("cost_usd") or 0) + 0.01
      for pos in list(store.open_positions("EV1")):
        store.close_position(pos["id"])
        store.log_trade({
          "event_ticker": "EV1",
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

    summary = store.hour_interval_summary("EV1")
    assert summary["open_exposure_usd"] == 0.0
    assert summary["total_entered_usd"] >= cumulative
    if summary["enter_count"] >= 2:
      assert summary["total_entered_usd"] > max_cap


def test_auto_stop_when_hour_bankroll_exhausted():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    max_cap = 25.0
    store.save_settings(HourlyBotSettings(enabled=True, max_spend_per_hour_usd=max_cap, mode="live"))
    store.log_trade({
      "event_ticker": "EV1",
      "action": "exit",
      "status": "filled",
      "pnl_usd": -25.0,
    })
    assert store.remaining_budget_usd("EV1", max_cap) == 0.0
    bot = HourlyBot(store, asset="btc")
    tab = _live_tab(event="EV1")
    actions = bot.run_continuous_cycle(
      tab, cfg={"hourly": {"regime": {"min_edge": 0.05, "min_expected_move_pct": 0.12}}}
    )
    assert any(a.get("action") == "auto_stop" for a in actions)
    assert store.get_settings().enabled
    assert store.get_settings().auto_stopped
    st = store.status("EV1")
    assert st["auto_stopped"] is True
    assert st["last_skip_reason"] == "auto_stopped_budget_exhausted"
    assert "exhausted" in (st["auto_stop_reason"] or "").lower()


def test_no_entries_after_auto_stop():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.save_settings(HourlyBotSettings(
      enabled=True, auto_stopped=True, max_spend_per_hour_usd=25.0,
    ))
    bot = HourlyBot(store, asset="btc")
    tab = _live_tab(event="EV1")
    assert bot.run_continuous_cycle(tab) == []
    assert store.last_skip_reason() == "auto_stopped_budget_exhausted"


def test_manual_reenable_after_auto_stop():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.save_settings(HourlyBotSettings(
      enabled=True, auto_stopped=True, max_spend_per_hour_usd=25.0,
    ))
    store.log_trade({
      "event_ticker": "EV1",
      "action": "exit",
      "status": "filled",
      "pnl_usd": -25.0,
    })
    store.save_settings(HourlyBotSettings(
      enabled=True, auto_stopped=False, max_spend_per_hour_usd=50.0,
    ))
    store.reset_paper_bankroll(50.0)
    bot = HourlyBot(store, asset="btc")
    tab = _live_tab(event="EV1")
    actions = bot.run_continuous_cycle(
      tab, cfg={"hourly": {"regime": {"min_edge": 0.05, "min_expected_move_pct": 0.12}}}
    )
    assert any(a.get("action") == "enter" for a in actions)
    assert store.get_settings().enabled


def test_auto_stop_clears_on_new_hour():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.save_settings(HourlyBotSettings(
      enabled=True, auto_stopped=True, max_spend_per_hour_usd=25.0, mode="live",
    ))
    store.sync_period("EV1", store.get_settings())
    bot = HourlyBot(store, asset="btc")
    tab = _live_tab(event="EV2")
    actions = bot.run_continuous_cycle(
      tab, cfg={"hourly": {"regime": {"min_edge": 0.05, "min_expected_move_pct": 0.12}}}
    )
    assert not store.get_settings().auto_stopped
    assert any(a.get("action") == "enter" for a in actions)


def test_free_mode_enters_from_range_contract_row():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.save_settings(HourlyBotSettings(
      enabled=True, max_spend_per_hour_usd=10.0, allow_strong=False, allow_actionable=False,
    ))
    bot = HourlyBot(store, asset="btc")
    tab = _live_tab(regime_allow=False)
    tab["live"]["primary_pick"] = {"ticker": "NEUTRAL-T", "signal": "NEUTRAL", "kalshi_mid": 0.5, "edge": 0.0}
    tab["live"]["strategy_threshold"] = {"best_edge": None, "most_likely": None, "contracts": []}
    band = {
      "ticker": "KXTEST-BAND",
      "signal": "BUY YES",
      "label": "$2,500–$2,519",
      "kalshi_mid": 0.35,
      "yes_bid": 0.35,
      "yes_ask": 0.35,
      "edge": 0.15,
      "model_prob": 0.50,
    }
    tab["live"]["strategy_range"] = {
      "best_edge": None,
      "most_likely": None,
      "contracts": [band],
    }
    actions = bot.run_continuous_cycle(tab, cfg={"hourly": {"regime": {}, "intrahour": {"enabled": False}}})
    assert len(actions) == 1
    assert actions[0]["action"] == "enter"
    assert actions[0]["market_ticker"] == "KXTEST-BAND"


def test_skips_when_no_book_quotes_available():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.save_settings(HourlyBotSettings(
      enabled=True, max_spend_per_hour_usd=10.0, allow_strong=False, allow_actionable=False,
    ))
    bot = HourlyBot(store, asset="btc")
    tab = _live_tab(regime_allow=False)
    tab["live"]["primary_pick"] = {
      "ticker": "KXTEST-T1",
      "signal": "BUY YES",
      "label": "$2,500+",
      "edge": 0.12,
    }
    actions = bot.run_continuous_cycle(tab, cfg={"hourly": {"regime": {}, "intrahour": {"enabled": False}}})
    assert actions == []
    assert store.last_skip_reason() == "no_liquidity"


def test_profit_target_exit_on_hold_alert():
  """30% mark gain on $10 leg exits at 25% threshold even when alert is HOLD."""
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.save_settings(HourlyBotSettings(
      enabled=True,
      max_spend_per_hour_usd=25.0,
      take_profit_pct=0.25,
      min_hold_seconds=0,
    ))
    store.open_position({
      "id": "p1",
      "event_ticker": "KXTEST-1H",
      "market_ticker": "KXTEST-T1",
      "side": "yes",
      "contracts": 25,
      "entry_price_cents": 40,
      "cost_usd": 10.0,
      "signal": "BUY YES",
      "entry_edge": 0.12,
      "opened_at": _opened_at_seconds_ago(60),
    })
    bot = HourlyBot(store, asset="btc")
    tab = _live_tab()
    tab["live"]["primary_pick"]["kalshi_mid"] = 0.52
    tab["live"]["primary_pick"]["yes_bid"] = 0.52
    tab["live"]["primary_pick"]["yes_ask"] = 0.52
    tab["live"]["primary_pick"]["edge"] = 0.12
    tab["live"]["regime"] = {"allow_trade": True, "reasons": []}
    actions = bot.run_continuous_cycle(tab, cfg={"hourly": {"regime": {"min_edge": 0.05}}})
    exits = [a for a in actions if a.get("action") == "exit"]
    assert len(exits) == 1
    assert "PROFIT TARGET" in exits[0]["detail"]
    assert exits[0]["pnl_usd"] == 3.0
    assert store.open_exposure_usd("KXTEST-1H") == 0.0


def test_profit_target_increases_hour_bankroll():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.save_settings(HourlyBotSettings(
      enabled=True, max_spend_per_hour_usd=25.0, take_profit_pct=0.25, min_hold_seconds=0,
      use_accumulated_profit=True,
    ))
    store.open_position({
      "id": "p1",
      "event_ticker": "KXTEST-1H",
      "market_ticker": "KXTEST-T1",
      "side": "yes",
      "contracts": 25,
      "entry_price_cents": 40,
      "cost_usd": 10.0,
      "signal": "BUY YES",
      "opened_at": _opened_at_seconds_ago(60),
    })
    bot = HourlyBot(store, asset="btc")
    tab = _live_tab()
    tab["live"]["primary_pick"]["kalshi_mid"] = 0.52
    tab["live"]["primary_pick"]["yes_bid"] = 0.52
    tab["live"]["primary_pick"]["yes_ask"] = 0.52
    bot.run_continuous_cycle(tab, cfg={"hourly": {"regime": {"min_edge": 0.05}}})
    assert store.realized_pnl_usd("KXTEST-1H") == 3.0
    settings = store.get_settings()
    assert store.hour_bankroll_usd("KXTEST-1H", 25.0, settings) == 28.0


def test_no_exit_when_profit_below_threshold():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.save_settings(HourlyBotSettings(
      enabled=True, max_spend_per_hour_usd=25.0, take_profit_pct=0.25, min_hold_seconds=0,
    ))
    store.open_position({
      "id": "p1",
      "event_ticker": "KXTEST-1H",
      "market_ticker": "KXTEST-T1",
      "side": "yes",
      "contracts": 25,
      "entry_price_cents": 40,
      "cost_usd": 10.0,
      "signal": "BUY YES",
      "opened_at": _opened_at_seconds_ago(60),
    })
    bot = HourlyBot(store, asset="btc")
    tab = _live_tab()
    tab["live"]["primary_pick"]["kalshi_mid"] = 0.44
    tab["live"]["primary_pick"]["yes_bid"] = 0.44
    tab["live"]["primary_pick"]["yes_ask"] = 0.44
    actions = bot.run_continuous_cycle(tab, cfg={"hourly": {"regime": {"min_edge": 0.05}}})
    assert not any(a.get("action") == "exit" for a in actions)
    assert len(store.open_positions("KXTEST-1H")) == 1


def test_profit_trail_exit_on_giveback_from_peak():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.save_settings(HourlyBotSettings(
      enabled=True,
      max_spend_per_hour_usd=25.0,
      take_profit_mode="hybrid",
      trail_giveback_pct=0.40,
      min_hold_seconds=0,
    ))
    store.open_position({
      "id": "p1",
      "event_ticker": "KXTEST-1H",
      "market_ticker": "KXTEST-T1",
      "side": "yes",
      "contracts": 25,
      "entry_price_cents": 40,
      "cost_usd": 10.0,
      "signal": "BUY YES",
      "opened_at": _opened_at_seconds_ago(60),
    })
    store.update_position_peaks("p1", 5.0, 10.0)
    bot = HourlyBot(store, asset="btc")
    tab = _live_tab()
    tab["live"]["primary_pick"]["kalshi_mid"] = 0.50
    tab["live"]["primary_pick"]["yes_bid"] = 0.50
    tab["live"]["primary_pick"]["yes_ask"] = 0.50
    actions = bot.run_continuous_cycle(tab, cfg={"hourly": {"regime": {"min_edge": 0.05}}})
    exits = [a for a in actions if a.get("action") == "exit"]
    assert len(exits) == 1
    assert "PROFIT TRAIL" in exits[0]["detail"]
    assert "peak +$5.00" in exits[0]["detail"]


def test_profit_trail_exits_without_hitting_fixed_target():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.save_settings(HourlyBotSettings(
      enabled=True,
      max_spend_per_hour_usd=25.0,
      take_profit_mode="trailing",
      trail_giveback_pct=0.35,
      min_hold_seconds=0,
    ))
    store.open_position({
      "id": "p1",
      "event_ticker": "KXTEST-1H",
      "market_ticker": "KXTEST-T1",
      "side": "yes",
      "contracts": 25,
      "entry_price_cents": 40,
      "cost_usd": 10.0,
      "signal": "BUY YES",
      "opened_at": _opened_at_seconds_ago(60),
    })
    store.update_position_peaks("p1", 1.2, 10.0)
    bot = HourlyBot(store, asset="btc")
    tab = _live_tab()
    tab["live"]["primary_pick"]["kalshi_mid"] = 0.42
    tab["live"]["primary_pick"]["yes_bid"] = 0.42
    tab["live"]["primary_pick"]["yes_ask"] = 0.42
    actions = bot.run_continuous_cycle(tab, cfg={"hourly": {"regime": {"min_edge": 0.05}}})
    exits = [a for a in actions if a.get("action") == "exit"]
    assert len(exits) == 1
    assert "PROFIT TRAIL" in exits[0]["detail"]
