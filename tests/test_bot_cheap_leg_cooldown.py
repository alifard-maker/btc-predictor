"""Tests for CHEAP LEG CUT re-entry cooldown guardrail."""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.trading.bot_cheap_leg_cooldown import (
  DEFAULT_CHEAP_LEG_CUT_COOLDOWN_SECONDS,
  cheap_leg_cut_cooldown_seconds,
  is_cheap_leg_cut_reason,
  market_identity_label,
  resolve_exit_cooldown_seconds,
)
from src.trading.hourly_bot import HourlyBot
from src.trading.hourly_bot_store import HourlyBotSettings, HourlyBotStore
from src.trading.slot15_bot import Slot15Bot
from src.trading.slot15_bot_store import Slot15BotSettings, Slot15BotStore


def test_is_cheap_leg_cut_reason():
  assert is_cheap_leg_cut_reason("CHEAP LEG CUT LOSS")
  assert not is_cheap_leg_cut_reason("LEG STOP")
  assert not is_cheap_leg_cut_reason(None)


def test_market_identity_label_prefers_label():
  assert market_identity_label("Open LONG", "KXBTC15M-TEST") == "Open LONG"
  assert market_identity_label(None, "KXBTC15M-TEST") == "KXBTC15M-TEST"
  assert market_identity_label("  ", "KXBTC15M-TEST") == "KXBTC15M-TEST"


def test_cheap_leg_cut_cooldown_seconds_from_config():
  cfg = {"hourly": {"bot": {"cheap_leg_cut_cooldown_seconds": 600}}}
  assert cheap_leg_cut_cooldown_seconds(cfg, kind="hourly") == 600
  assert cheap_leg_cut_cooldown_seconds(None, kind="hourly") == DEFAULT_CHEAP_LEG_CUT_COOLDOWN_SECONDS


def test_resolve_exit_cooldown_seconds_ignores_aggressive_reentry():
  settings = HourlyBotSettings(
    reentry_cooldown_seconds=30,
    profit_exit_cooldown_seconds=30,
    aggressive_entries=True,
  )
  cfg = {"hourly": {"bot": {"cheap_leg_cut_cooldown_seconds": 300}}}
  assert resolve_exit_cooldown_seconds(
    settings, "CHEAP LEG CUT LOSS", cfg, bot_kind="hourly",
  ) == 300
  assert resolve_exit_cooldown_seconds(
    settings, "PROFIT TARGET", cfg, bot_kind="hourly",
  ) == 30
  assert resolve_exit_cooldown_seconds(
    settings, "CUT LOSSES", cfg, bot_kind="hourly",
  ) == 30


def test_store_cheap_leg_cut_cooldown_blocks_and_expires():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.record_cheap_leg_cut_cooldown(
      "KXTEST-1H",
      label="$2,500+",
      market_ticker="KXTEST-T1",
      cooldown_seconds=300,
    )
    assert store.is_in_cheap_leg_cut_cooldown(
      "KXTEST-1H",
      label="$2,500+",
      market_ticker="KXTEST-T1",
      cooldown_seconds=300,
    )
    past = (datetime.now(timezone.utc) - timedelta(seconds=400)).isoformat()
    store.record_cheap_leg_cut_cooldown(
      "KXTEST-1H",
      label="$2,500+",
      market_ticker="KXTEST-T1",
      cooldown_seconds=300,
      exited_at=past,
    )
    assert not store.is_in_cheap_leg_cut_cooldown(
      "KXTEST-1H",
      label="$2,500+",
      market_ticker="KXTEST-T1",
      cooldown_seconds=300,
    )


def _hourly_live_tab():
  pick = {
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
    "event": {"event_ticker": "KXTEST-1H"},
    "live": {
      "primary_pick": pick,
      "current_price": 2500.0,
      "terminal_mu": 2510.0,
      "regime": {"allow_trade": True, "reasons": []},
      "strategy_threshold": {"best_edge": pick, "most_likely": pick, "contracts": [pick]},
      "strategy_range": {"best_edge": None, "most_likely": None, "contracts": []},
    },
    "locked": {"reference_price": 2495.0, "terminal_mu": 2505.0, "primary_pick": pick},
    "brti_live": 2500.0,
  }


def test_hourly_cheap_leg_cut_blocks_reentry_despite_aggressive_reentry():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.save_settings(HourlyBotSettings(
      enabled=True,
      max_spend_per_hour_usd=25.0,
      aggressive_entries=True,
    ))
    store.open_position({
      "id": "p1",
      "event_ticker": "KXTEST-1H",
      "market_ticker": "KXTEST-T1",
      "side": "no",
      "contracts": 10,
      "entry_price_cents": 16,
      "cost_usd": 1.6,
      "signal": "BUY NO",
      "label": "$2,500+",
      "entry_edge": 0.12,
    })
    bot = HourlyBot(store, asset="btc")
    cfg = {
      "hourly": {
        "bot": {
          "cheap_leg_max_entry_cents": 20,
          "cheap_leg_cut_loss_cents": 10,
          "cheap_leg_cut_cooldown_seconds": 300,
        },
        "regime": {},
      }
    }
    tab = {
      "ok": True,
      "event": {"event_ticker": "KXTEST-1H"},
      "live": {
        "current_price": 100000,
        "hours_to_settle": 0.5,
        "regime": {"allow_trade": True, "reasons": []},
        "primary_pick": {
          "ticker": "KXTEST-T1",
          "signal": "BUY YES",
          "label": "$2,500+",
          "edge": 0.12,
          "kalshi_mid": 0.92,
          "yes_bid": 0.92,
          "yes_ask": 0.92,
        },
      },
      "locked": {},
    }
    exits = bot.run_continuous_cycle(tab, cfg=cfg)
    assert len(exits) == 1
    assert "CHEAP LEG CUT LOSS" in (exits[0].get("detail") or "")

    with store._connect() as conn:
      conn.execute("DELETE FROM bot_cooldowns")

    tab["live"]["primary_pick"]["kalshi_mid"] = 0.40
    tab["live"]["primary_pick"]["yes_bid"] = 0.40
    tab["live"]["primary_pick"]["yes_ask"] = 0.40
    tab["live"]["primary_pick"]["signal"] = "BUY YES"
    actions = bot.run_continuous_cycle(tab, cfg=cfg)
    assert actions == []
    assert store.last_skip_reason() == "cheap_leg_cut_cooldown:$2,500+"


def test_slot15_cheap_leg_cut_blocks_same_label_reentry():
  with tempfile.TemporaryDirectory() as tmp:
    store = Slot15BotStore(Path(tmp) / "slot15_bot_btc.db")
    store.save_settings(Slot15BotSettings(
      enabled=True,
      max_spend_per_slot_usd=25.0,
      aggressive_entries=True,
    ))
    slot_key = "2025-06-28T14:00:00-04:00"
    store.open_position({
      "id": "p1",
      "event_ticker": slot_key,
      "market_ticker": "KXBTC15M-TEST",
      "side": "yes",
      "contracts": 10,
      "entry_price_cents": 13,
      "cost_usd": 1.3,
      "signal": "LONG",
      "label": "Open LONG",
    })
    bot = Slot15Bot(store, asset="btc")
    tab = {
      "ok": True,
      "slot_key": slot_key,
      "prediction": {"signal": "LONG", "prob_up": 0.62},
      "monitor": {"action": "HOLD", "message": "ok", "seconds_remaining": 600},
      "kalshi": {
        "market_ticker": "KXBTC15M-TEST",
        "yes_mid": 0.10,
        "yes_bid": 0.10,
        "yes_ask": 0.10,
      },
      "bet_assessment": {"actionable_bet": True, "actionable_tone": "strong"},
    }
    cfg = {
      "intra_slot": {
        "bot": {
          "cheap_leg_max_entry_cents": 20,
          "cheap_leg_cut_loss_cents": 10,
          "cheap_leg_cut_cooldown_seconds": 300,
        }
      }
    }
    exits = bot.run_continuous_cycle(tab, cfg=cfg)
    assert len(exits) == 1
    assert "CHEAP LEG CUT LOSS" in (exits[0].get("detail") or "")

    with store._connect() as conn:
      conn.execute("DELETE FROM bot_cooldowns")

    tab["kalshi"]["yes_mid"] = 0.55
    tab["kalshi"]["yes_bid"] = 0.55
    tab["kalshi"]["yes_ask"] = 0.55
    actions = bot.run_continuous_cycle(tab, cfg=cfg)
    assert actions == []
    assert store.last_skip_reason() == "cheap_leg_cut_cooldown:Open LONG"


def test_hourly_trial_cheap_leg_cut_blocks_reentry():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "hourly_trial_bot_btc.db")
    store.save_settings(HourlyBotSettings(
      enabled=True,
      max_spend_per_hour_usd=25.0,
      aggressive_entries=True,
    ))
    store.open_position({
      "id": "p1",
      "event_ticker": "KXTEST-1H",
      "market_ticker": "KXTEST-T1",
      "side": "yes",
      "contracts": 25,
      "entry_price_cents": 13,
      "cost_usd": 3.25,
      "signal": "BUY YES",
      "label": "$60,300+",
      "contract_type": "threshold",
      "strike_type": "greater",
      "floor_strike": 60300.0,
    })
    bot = HourlyBot(store, asset="btc", kind="hourly_trial")
    pick = {
      "ticker": "KXTEST-T1",
      "signal": "BUY YES",
      "label": "$60,300+",
      "edge": 0.08,
      "contract_type": "threshold",
      "strike_type": "greater",
      "floor_strike": 60300.0,
      "kalshi_mid": 0.10,
      "yes_bid": 0.10,
      "yes_ask": 0.10,
    }
    tab = _hourly_live_tab()
    tab["live"]["primary_pick"] = pick
    tab["live"]["strategy_threshold"]["contracts"] = [pick]
    tab["brti_live"] = 60237.25
    tab["live"]["current_price"] = 60237.25
    tab["live"]["hours_to_settle"] = 0.20
    cfg = {
      "hourly": {
        "bot": {
          "cheap_leg_max_entry_cents": 20,
          "cheap_leg_cut_loss_cents": 10,
          "cheap_leg_cut_cooldown_seconds": 300,
        },
        "regime": {},
      }
    }
    exits = [a for a in bot.run_continuous_cycle(tab, cfg=cfg) if a.get("action") == "exit"]
    assert len(exits) == 1
    assert "CHEAP LEG CUT LOSS" in (exits[0].get("detail") or "")

    with store._connect() as conn:
      conn.execute("DELETE FROM bot_cooldowns")

    tab["live"]["primary_pick"]["kalshi_mid"] = 0.40
    tab["live"]["primary_pick"]["yes_bid"] = 0.40
    tab["live"]["primary_pick"]["yes_ask"] = 0.40
    actions = bot.run_continuous_cycle(tab, cfg=cfg)
    assert actions == []
    assert store.last_skip_reason() == "cheap_leg_cut_cooldown:$60,300+"
