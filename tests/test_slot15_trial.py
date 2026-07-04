"""ETH 15m paper trial bot store, compare API, scheduler, and dashboard wiring."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from src.assets import asset_cfg
from src.config import load_config
from src.scheduler.loop import PredictionLoop
from src.trading.hourly_live_trial_compare import build_slot15_live_trial_compare
from src.trading.slot15_bot_store import Slot15BotSettings, Slot15BotStore


def _log_slot15_trade(store: Slot15BotStore, **kwargs):
  base = {
    "event_ticker": "KXETH15M-26JUL041200",
    "trigger": "continuous",
    "action": "enter",
    "mode": "live",
    "market_ticker": "KXETH15M-26JUL041200-T1600",
    "side": "yes",
    "contracts": 5,
    "price_cents": 42,
    "entry_price_cents": 42,
    "cost_usd": 2.10,
    "status": "filled",
    "label": "≥ $1,600",
  }
  base.update(kwargs)
  store.log_trade(base)


def test_slot15_trial_compare_matched_slot_pnl(tmp_path: Path):
  live_db = tmp_path / "slot15_bot_eth.db"
  trial_db = tmp_path / "slot15_trial_bot_eth.db"
  live_store = Slot15BotStore(live_db)
  trial_store = Slot15BotStore(trial_db)
  live_store.save_settings(Slot15BotSettings(enabled=True, mode="live"))

  _log_slot15_trade(
    live_store,
    created_at="2026-07-04T12:05:00+00:00",
    mode="live",
  )
  _log_slot15_trade(
    live_store,
    action="exit",
    exit_price_cents=55,
    pnl_usd=0.65,
    created_at="2026-07-04T12:10:00+00:00",
    mode="live",
  )
  _log_slot15_trade(
    trial_store,
    created_at="2026-07-04T12:06:00+00:00",
    mode="paper",
  )
  _log_slot15_trade(
    trial_store,
    action="exit",
    exit_price_cents=48,
    pnl_usd=0.30,
    created_at="2026-07-04T12:09:00+00:00",
    mode="paper",
  )

  out = build_slot15_live_trial_compare(
    live_store,
    trial_store,
    asset="eth",
    limit_slots=5,
  )

  assert out["ok"] is True
  assert out["live_kind"] == "slot15"
  assert out["trial_kind"] == "slot15_trial"
  assert out["matched_event_count"] == 1
  assert len(out["hours"]) >= 1
  slot = out["hours"][0]
  assert slot["event_ticker"] == "KXETH15M-26JUL041200"
  assert slot["both_active"] is True
  assert slot["live"]["net_pnl_usd"] == 0.65
  assert slot["trial"]["net_pnl_usd"] == 0.30
  assert slot["pnl_delta_usd"] == 0.35


def test_slot15_compare_uses_main_bot_paper_mode(tmp_path: Path):
  live_db = tmp_path / "slot15_bot_eth.db"
  trial_db = tmp_path / "slot15_trial_bot_eth.db"
  live_store = Slot15BotStore(live_db)
  trial_store = Slot15BotStore(trial_db)
  live_store.save_settings(Slot15BotSettings(enabled=True, mode="paper"))

  _log_slot15_trade(
    live_store,
    created_at="2026-07-04T13:05:00+00:00",
    mode="paper",
  )
  _log_slot15_trade(
    trial_store,
    created_at="2026-07-04T13:06:00+00:00",
    mode="paper",
  )

  out = build_slot15_live_trial_compare(
    live_store,
    trial_store,
    asset="eth",
    limit_slots=5,
  )

  assert out["live_mode"] == "paper"
  assert out["matched_event_count"] == 1
  assert out["hours"][0]["both_active"] is True


def test_slot15_trial_bot_store_separate_from_live(tmp_path: Path):
  with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    live = Slot15BotStore(root / "slot15_bot_eth.db")
    trial = Slot15BotStore(root / "slot15_trial_bot_eth.db")
    live.save_settings(Slot15BotSettings(enabled=True, mode="live"))
    trial.save_settings(Slot15BotSettings(enabled=True, mode="paper"))
    assert live.db_path.name == "slot15_bot_eth.db"
    assert trial.db_path.name == "slot15_trial_bot_eth.db"
    assert live.get_settings().mode == "live"
    assert trial.get_settings().mode == "paper"


def test_loop_slot15_trial_store_path(monkeypatch):
  with tempfile.TemporaryDirectory() as tmp:
    data_dir = Path(tmp) / "persist"
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    cfg = load_config()
    loop = PredictionLoop(cfg)
    trial_db = loop.slot15_trial_bot_store("eth").db_path
    live_db = loop.slot15_bot_store("eth").db_path
    assert trial_db.name == "slot15_trial_bot_eth.db"
    assert live_db.name == "slot15_bot_eth.db"
    assert str(trial_db) != str(live_db)


def test_eth_intra_slot_trial_continuous_enabled():
  cfg = load_config()
  eth_bot = asset_cfg(cfg, "eth")["intra_slot"]["bot"]
  trial = eth_bot.get("trial") or {}
  assert trial.get("continuous_enabled") is True


def test_scheduler_registers_eth_slot15_trial_job(monkeypatch):
  with tempfile.TemporaryDirectory() as tmp:
    monkeypatch.setenv("DATA_DIR", str(Path(tmp) / "data"))
    cfg = load_config()
    loop = PredictionLoop(cfg)
    scheduler = MagicMock()
    added = []

    def capture_add_job(fn, trigger, **kwargs):
      added.append(kwargs.get("id"))

    scheduler.add_job = capture_add_job
    loop._schedule_slot15_bot(scheduler)
    assert "eth_slot15_trial_bot_continuous" in added


def test_dashboard_eth_slot15_trial_api_paths():
  dashboard = Path(__file__).resolve().parents[1] / "src" / "api" / "static" / "dashboard.html"
  html = dashboard.read_text(encoding="utf-8")
  for needle in (
    "eth-slot15-trial-bot",
    "eth-slot15-live-trial-compare",
    "/api/eth/15m-trial/bot",
    "slot15TrialBotStatusUrl",
    "loadSlot15TrialBot",
    "loadSlot15LiveTrialCompare",
    "mergeSlot15TrialBotStatus",
    "renderSlot15TrialBot",
    "slot15_trial:eth",
    "/api/bots/slot15-live-trial-compare",
  ):
    assert needle in html, f"missing dashboard wiring: {needle}"
