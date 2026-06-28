"""Bot cycle metadata and Railway persistence paths."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from src.config import ensure_dirs, load_config
from src.scheduler.loop import PredictionLoop
from src.trading.hourly_bot_store import HourlyBotSettings, HourlyBotStore
from src.trading.slot15_bot_store import Slot15BotStore


def test_record_cycle_persists_in_sqlite():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.record_cycle(active=True)
    rt = store.get_runtime()
    assert rt["last_cycle_at"]
    assert rt["last_cycle_active"] is True
    assert rt["cycles_total"] == 1
    store.record_cycle(active=False)
    rt2 = store.get_runtime()
    assert rt2["cycles_total"] == 2
    assert rt2["last_cycle_active"] is False


def test_status_includes_server_runtime():
  with tempfile.TemporaryDirectory() as tmp:
    store = Slot15BotStore(Path(tmp) / "bot.db")
    store.record_cycle(active=True)
    st = store.status("SLOT1")
    assert st["server_runtime"]["cycles_total"] == 1


def test_bot_db_paths_use_data_dir(monkeypatch):
  with tempfile.TemporaryDirectory() as tmp:
    data_dir = Path(tmp) / "persist"
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    cfg = load_config()
    ensure_dirs(cfg)
    loop = PredictionLoop(cfg)
    hourly_db = loop.hourly_bot_store("btc").db_path
    slot_db = loop.slot15_bot_store("btc").db_path
    assert str(data_dir) in str(hourly_db)
    assert str(data_dir) in str(slot_db)
    assert hourly_db.name == "hourly_bot_btc.db"
    assert slot_db.name == "slot15_bot_btc.db"


def test_paper_bankroll_survives_store_reopen():
  with tempfile.TemporaryDirectory() as tmp:
    db = Path(tmp) / "bot.db"
    store = HourlyBotStore(db)
    store.save_settings(HourlyBotSettings(mode="paper", max_spend_per_hour_usd=25.0))
    store.log_trade({
      "event_ticker": "EV1",
      "action": "exit",
      "mode": "paper",
      "status": "filled",
      "pnl_usd": 7.0,
    })
    store2 = HourlyBotStore(db)
    settings = store2.get_settings()
    assert store2.hour_bankroll_usd("EV2", 25.0, settings) == 32.0
