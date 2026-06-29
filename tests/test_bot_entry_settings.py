"""Tests for entry settings snapshots, settings audit, and cheap-leg exits."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from src.backup.logs_backup import on_settings_saved
from src.trading.bot_entry_settings import (
  hourly_entry_settings_snapshot,
  infer_store_meta,
  slot15_entry_settings_snapshot,
)
from src.trading.bot_profit_exit import (
  CheapLegExitConfig,
  cheap_leg_exit_config,
  evaluate_cheap_leg_cut_loss,
)
from src.trading.hourly_bot import HourlyBot
from src.trading.hourly_bot_store import HourlyBotSettings, HourlyBotStore
from src.trading.slot15_bot import Slot15Bot
from src.trading.slot15_bot_store import Slot15BotSettings, Slot15BotStore


def test_hourly_entry_settings_snapshot_free_mode():
  snap = hourly_entry_settings_snapshot(
    HourlyBotSettings(
      enabled=True,
      mode="paper",
      max_spend_per_hour_usd=25.0,
      allow_strong=False,
      allow_actionable=False,
      use_accumulated_profit=True,
    )
  )
  assert snap["free_mode"] is True
  assert snap["max_spend"] == 25.0
  assert snap["use_accumulated_profit"] is True


def test_log_trade_persists_entry_settings_json():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "hourly_bot_btc.db")
    settings = HourlyBotSettings(enabled=True, max_spend_per_hour_usd=10.0)
    snap = hourly_entry_settings_snapshot(settings)
    row = store.log_trade({
      "event_ticker": "EV1",
      "action": "enter",
      "mode": "paper",
      "status": "filled",
      "price_cents": 40,
      "entry_settings": snap,
    })
    assert row.get("entry_settings") == snap
    listed = store.list_trades(limit=1)[0]
    assert listed["entry_settings"]["free_mode"] is True
    assert listed["entry_settings"]["max_spend"] == 10.0


def test_save_settings_appends_audit_jsonl():
  with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    logs = root / "logs"
    cfg = {
      "paths": {"logs": str(logs)},
      "log_backup": {"enabled": True, "backup_dir": str(root / "backups")},
    }
    store = HourlyBotStore(logs / "hourly_bot_btc.db")
    store.save_settings(
      HourlyBotSettings(enabled=True, max_spend_per_hour_usd=30.0),
      source="dashboard",
      cfg=cfg,
    )
    audit = root / "backups" / "paper" / "settings_audit.jsonl"
    assert audit.exists()
    record = json.loads(audit.read_text(encoding="utf-8").strip())
    assert record["source"] == "dashboard"
    assert record["bot_type"] == "hourly"
    assert record["asset"] == "btc"
    assert record["new_settings"]["max_spend_per_hour_usd"] == 30.0


def test_infer_store_meta_from_db_path():
  assert infer_store_meta("slot15_bot_eth.db") == ("eth", "slot15")
  assert infer_store_meta("hourly_bot_btc.db") == ("btc", "hourly")


def test_cheap_leg_cut_loss_exits_before_flat_pnl_guard():
  cfg = CheapLegExitConfig(max_entry_cents=20, cut_loss_cents=10)
  pos = {"entry_price_cents": 15, "side": "yes", "contracts": 10}
  reason, detail = evaluate_cheap_leg_cut_loss(pos, mark_cents=10, cfg=cfg)
  assert reason == "CHEAP LEG CUT LOSS"
  assert "15" in detail


def test_cheap_leg_skipped_for_expensive_entry():
  cfg = CheapLegExitConfig(max_entry_cents=20, cut_loss_cents=10)
  pos = {"entry_price_cents": 55, "side": "yes", "contracts": 10}
  assert evaluate_cheap_leg_cut_loss(pos, mark_cents=10, cfg=cfg) == (None, "")


def test_slot15_cheap_leg_exit_in_continuous_cycle():
  with tempfile.TemporaryDirectory() as tmp:
    store = Slot15BotStore(Path(tmp) / "slot15_bot_btc.db")
    store.save_settings(Slot15BotSettings(enabled=True, max_spend_per_slot_usd=25.0))
    slot_key = "2025-06-28T14:00:00-04:00"
    store.open_position({
      "id": "p1",
      "event_ticker": slot_key,
      "market_ticker": "KXBTC15M-TEST",
      "side": "yes",
      "contracts": 10,
      "entry_price_cents": 18,
      "cost_usd": 1.8,
      "signal": "LONG",
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
    cfg = {"intra_slot": {"bot": {"cheap_leg_max_entry_cents": 20, "cheap_leg_cut_loss_cents": 10}}}
    actions = bot.run_continuous_cycle(tab, cfg=cfg)
    assert len(actions) == 1
    assert actions[0]["action"] == "exit"
    assert "CHEAP LEG CUT LOSS" in (actions[0].get("detail") or "")


def test_hourly_cheap_leg_exit_in_continuous_cycle():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "hourly_bot_btc.db")
    store.save_settings(HourlyBotSettings(enabled=True, max_spend_per_hour_usd=25.0))
    store.open_position({
      "id": "p1",
      "event_ticker": "KXTEST-1H",
      "market_ticker": "KXTEST-T1",
      "side": "no",
      "contracts": 10,
      "entry_price_cents": 16,
      "cost_usd": 1.6,
      "signal": "BUY NO",
      "entry_edge": 0.12,
    })
    bot = HourlyBot(store, asset="btc")
    tab = {
      "ok": True,
      "event": {"event_ticker": "KXTEST-1H"},
      "live": {
        "current_price": 100000,
        "hours_to_settle": 0.5,
        "regime": {"allow_trade": True, "reasons": []},
        "primary_pick": {
          "ticker": "KXTEST-T1",
          "signal": "BUY NO",
          "edge": 0.12,
          "kalshi_mid": 0.92,
          "yes_bid": 0.92,
          "yes_ask": 0.92,
        },
      },
      "locked": {},
    }
    cfg = {"hourly": {"bot": {"cheap_leg_max_entry_cents": 20, "cheap_leg_cut_loss_cents": 10}, "regime": {}}}
    actions = bot.run_continuous_cycle(tab, cfg=cfg)
    assert len(actions) == 1
    assert actions[0]["action"] == "exit"
    assert "CHEAP LEG CUT LOSS" in (actions[0].get("detail") or "")


def test_cheap_leg_config_defaults():
  cfg = cheap_leg_exit_config({"hourly": {"bot": {}}}, kind="hourly")
  assert cfg.max_entry_cents == 20
  assert cfg.cut_loss_cents == 10
