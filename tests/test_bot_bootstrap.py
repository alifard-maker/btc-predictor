"""PAPER_BOT_AUTO_ENABLE env bootstrap."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from src.trading.bot_bootstrap import bootstrap_paper_bots, parse_auto_enable_tokens
from src.trading.hourly_bot_store import HourlyBotSettings, HourlyBotStore
from src.trading.slot15_bot_store import Slot15BotSettings, Slot15BotStore


def test_parse_auto_enable_tokens():
  assert parse_auto_enable_tokens("btc,eth,slot15") == [
    ("hourly", "btc"),
    ("hourly", "eth"),
    ("slot15", "btc"),
  ]
  assert ("slot15", "eth") in parse_auto_enable_tokens("all")


def test_bootstrap_enables_fresh_hourly_bot(monkeypatch):
  with tempfile.TemporaryDirectory() as tmp:
    db = Path(tmp) / "hourly.db"
    store = HourlyBotStore(db)
    loop = MagicMock()
    loop.cfg = {}
    loop.hourly_bot_store = lambda asset: store if asset == "btc" else store
    loop.slot15_bot_store = MagicMock()
    loop._slot15m_enabled = lambda asset: False

    monkeypatch.setenv("PAPER_BOT_AUTO_ENABLE", "btc")
    activated = bootstrap_paper_bots(loop)
    assert activated == ["btc-hourly"]
    settings = store.get_settings()
    assert settings.enabled is True
    assert settings.mode == "paper"


def test_bootstrap_skips_when_trades_exist(monkeypatch):
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "hourly.db")
    store.log_trade({
      "event_ticker": "EV1",
      "action": "enter",
      "mode": "paper",
      "status": "filled",
      "cost_usd": 5.0,
    })
    loop = MagicMock()
    loop.cfg = {}
    loop.hourly_bot_store = lambda asset: store
    loop.slot15_bot_store = MagicMock()
    loop._slot15m_enabled = lambda asset: False

    monkeypatch.setenv("PAPER_BOT_AUTO_ENABLE", "btc")
    assert bootstrap_paper_bots(loop) == []
    assert store.get_settings().enabled is False


def test_bootstrap_skips_when_already_enabled(monkeypatch):
  with tempfile.TemporaryDirectory() as tmp:
    store = Slot15BotStore(Path(tmp) / "slot15.db")
    store.save_settings(Slot15BotSettings(enabled=True, mode="paper"))
    loop = MagicMock()
    loop.cfg = {}
    loop.hourly_bot_store = MagicMock()
    loop.slot15_bot_store = lambda asset: store
    loop._slot15m_enabled = lambda asset: True

    monkeypatch.setenv("PAPER_BOT_AUTO_ENABLE", "slot15")
    assert bootstrap_paper_bots(loop) == []
