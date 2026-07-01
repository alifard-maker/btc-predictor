"""Tests for live exit guards and reconcile hygiene."""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

from src.trading.bot_live_exit import (
  LiveExitConfig,
  allow_live_cut_loss,
  apply_live_exit_entry_guards,
  live_exit_config,
  overlay_live_profit_settings,
  reconcile_close_blocked,
)
from src.trading.entry_strategy import EntryStrategyConfig
from src.trading.hourly_bot_store import HourlyBotSettings, HourlyBotStore
from src.trading.live_position_sync import (
  should_reconcile_close_live_leg,
  sync_live_positions_from_kalshi,
)


def test_live_exit_config_reads_hourly_section():
  cfg = {"hourly": {"bot": {"live_exit": {"cut_loss_min_usd": 0.35}}}}
  assert live_exit_config(cfg, kind="hourly").cut_loss_min_usd == 0.35


def test_apply_live_exit_entry_guards_blocks_tail_in_live():
  estrat = EntryStrategyConfig(tail_entry_block=False, tail_entry_max_cents=20)
  out = apply_live_exit_entry_guards(estrat, {}, mode="live", kind="hourly")
  assert out.tail_entry_block is True
  assert apply_live_exit_entry_guards(estrat, {}, mode="paper", kind="hourly") is estrat


def test_allow_live_cut_loss_blocks_small_loss_and_short_hold():
  pos = {
    "opened_at": datetime.now(timezone.utc).isoformat(),
    "entry_price_cents": 40,
  }
  cfg = {
    "hourly": {
      "bot": {
        "live_exit": {
          "cut_loss_min_usd": 0.20,
          "cut_loss_min_hold_seconds": 120,
        }
      }
    }
  }
  assert not allow_live_cut_loss(
    exit_reason="CUT LOSSES",
    unrealized_usd=-0.10,
    pos=pos,
    settings_min_hold=60,
    cfg=cfg,
    kind="hourly",
  )
  assert not allow_live_cut_loss(
    exit_reason="CUT LOSSES",
    unrealized_usd=-0.50,
    pos=pos,
    settings_min_hold=60,
    cfg=cfg,
    kind="hourly",
  )
  old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
  assert allow_live_cut_loss(
    exit_reason="CUT LOSSES",
    unrealized_usd=-0.50,
    pos={"opened_at": old, "entry_price_cents": 40},
    settings_min_hold=60,
    cfg=cfg,
    kind="hourly",
  )


def test_allow_live_cut_loss_blocks_profitable_cut():
  cfg = {"hourly": {"bot": {"live_exit": {"block_cut_when_profitable": True}}}}
  assert not allow_live_cut_loss(
    exit_reason="CUT LOSSES",
    unrealized_usd=0.02,
    pos={"opened_at": datetime.now(timezone.utc).isoformat()},
    settings_min_hold=0,
    cfg=cfg,
    kind="hourly",
  )


def test_allow_live_cut_loss_stricter_for_adopted_legs():
  cfg = {
    "hourly": {
      "bot": {
        "live_exit": {
          "cut_loss_min_usd": 0.20,
          "cut_loss_min_hold_seconds": 120,
          "adopted_leg_cut_loss_min_usd": 0.50,
          "adopted_leg_cut_loss_min_hold_seconds": 300,
        }
      }
    }
  }
  pos = {
    "opened_at": (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat(),
    "entry_source": "adopted_resting",
  }
  assert not allow_live_cut_loss(
    exit_reason="CUT LOSSES",
    unrealized_usd=-0.40,
    pos=pos,
    settings_min_hold=60,
    cfg=cfg,
    kind="hourly",
  )
  old = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
  assert allow_live_cut_loss(
    exit_reason="CUT LOSSES",
    unrealized_usd=-0.55,
    pos={**pos, "opened_at": old},
    settings_min_hold=60,
    cfg=cfg,
    kind="hourly",
  )


def test_overlay_live_profit_settings_lowers_mid_price_tp():
  settings = HourlyBotSettings(take_profit_usd=0.0, profit_exit_cooldown_seconds=60)
  pos = {"entry_price_cents": 42}
  cfg = {
    "hourly": {
      "bot": {
        "live_exit": {
          "take_profit_usd": 0.08,
          "profit_exit_cooldown_seconds": 30,
          "mid_price_take_profit_usd": 0.06,
          "mid_price_max_entry_cents": 60,
        }
      }
    }
  }
  out = overlay_live_profit_settings(settings, pos, cfg, mode="live", kind="hourly")
  assert out.take_profit_usd == 0.06
  assert out.profit_exit_cooldown_seconds == 30


def test_reconcile_close_blocked_for_young_position():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    pos = {
      "id": "p1",
      "event_ticker": "EV1",
      "market_ticker": "T1",
      "side": "yes",
      "opened_at": datetime.now(timezone.utc).isoformat(),
    }
    cfg = {"hourly": {"bot": {"live_exit": {"reconcile_min_position_age_seconds": 120}}}}
    assert reconcile_close_blocked(store, pos, cfg, kind="hourly") == "reconcile_min_age"


def test_sync_skips_reconcile_for_young_live_leg():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.open_position({
      "id": "p1",
      "event_ticker": "EV1",
      "market_ticker": "T1",
      "side": "yes",
      "contracts": 2,
      "entry_price_cents": 20,
      "cost_usd": 0.4,
      "mode": "live",
    })
    kalshi = MagicMock()
    kalshi.authenticated = True
    kalshi.get_market_position.return_value = 0
    kalshi.list_resting_orders.return_value = []
    cfg = {"hourly": {"bot": {"live_exit": {"reconcile_min_position_age_seconds": 600}}}}
    out = sync_live_positions_from_kalshi(store, kalshi, "EV1", cfg=cfg, kind="hourly")
    assert store.open_positions("EV1")
    assert any(c.get("action") == "inventory_pending_exit" for c in out["changes"])


def test_should_reconcile_close_respects_grace_config():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    pos = {
      "id": "p2",
      "event_ticker": "EV1",
      "market_ticker": "T1",
      "side": "yes",
      "opened_at": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
    }
    kalshi = MagicMock()
    kalshi.get_market_position.return_value = 0
    kalshi.list_resting_orders.return_value = []
    cfg = {"hourly": {"bot": {"live_exit": LiveExitConfig().__dict__}}}
    assert should_reconcile_close_live_leg(kalshi, store, pos, cfg=cfg, kind="hourly")
