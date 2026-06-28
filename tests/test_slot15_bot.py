"""Tests for 15m auto-bet bot store and helpers."""

from __future__ import annotations

import tempfile
from pathlib import Path

from src.trading.slot15_bot import Slot15Bot, bet_qualifies, _contracts_for_budget
from src.trading.slot15_bot_store import Slot15BotSettings, Slot15BotStore


def test_settings_roundtrip():
  with tempfile.TemporaryDirectory() as tmp:
    store = Slot15BotStore(Path(tmp) / "bot.db")
    store.save_settings(Slot15BotSettings(
      enabled=True,
      mode="live",
      max_spend_per_slot_usd=50.0,
      allow_strong=False,
      allow_actionable=True,
    ))
    s = store.get_settings()
    assert s.enabled is True
    assert s.mode == "live"
    assert s.max_spend_per_slot_usd == 50.0
    assert s.allow_strong is False


def test_contracts_for_budget():
  assert _contracts_for_budget(10.0, 50) == 20
  assert _contracts_for_budget(0.0, 50) == 0


def test_bet_qualifies_disabled():
  assert not bet_qualifies("LONG", {"actionable_bet": True, "actionable_tone": "strong"}, Slot15BotSettings(enabled=False))


def test_trade_log_persists_across_settings_change():
  with tempfile.TemporaryDirectory() as tmp:
    store = Slot15BotStore(Path(tmp) / "bot.db")
    store.log_trade({
      "event_ticker": "SLOT1",
      "action": "enter",
      "mode": "paper",
      "status": "filled",
      "cost_usd": 1.0,
      "entry_price_cents": 50,
    })
    store.save_settings(Slot15BotSettings(enabled=False))
    assert len(store.list_trades()) == 1


def test_status_includes_slot_summary():
  with tempfile.TemporaryDirectory() as tmp:
    store = Slot15BotStore(Path(tmp) / "bot.db")
    store.log_trade({
      "event_ticker": "SLOT1",
      "action": "enter",
      "status": "filled",
      "cost_usd": 3.0,
      "entry_price_cents": 30,
    })
    status = store.status("SLOT1")
    assert status["slot_summary"]["enter_count"] == 1
    assert status["max_spend_per_slot_usd"] == 25.0
