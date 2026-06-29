"""Tests for 15m exit context logging (BRTI/ERTI vet fields)."""

from __future__ import annotations

import tempfile
from pathlib import Path

from src.trading.slot15_bot import Slot15Bot
from src.trading.slot15_bot_store import Slot15BotSettings, Slot15BotStore
from src.trading.slot15_exit_context import (
  build_slot15_exit_context,
  format_slot15_exit_context_detail,
)


def test_build_slot15_exit_context_includes_brti_and_reassess():
  pos = {
    "side": "yes",
    "signal": "LONG",
    "reference_price": 100000.0,
    "market_ticker": "KXBTC15M-TEST",
  }
  tab = {
    "slot_label": "2:00–2:15 PM ET",
    "monitor": {
      "bet_side": "UP",
      "signal_at_open": "LONG",
      "reference_price": 100000.0,
      "current_price": 100062.0,
      "action": "HOLD",
      "reassessed_prob_up": 0.62,
      "reassess_summary": "62% UP at close",
      "seconds_remaining": 420,
    },
  }
  ctx = build_slot15_exit_context(
    pos=pos,
    tab=tab,
    unrealized_pnl_usd=-0.40,
    exit_reason="LEG STOP",
    asset="btc",
    leg_position_alert={
      "alert": "CUT LOSSES",
      "detail": "Mark drawdown",
      "slot_monitor_alert": "HOLD",
    },
  )
  assert ctx["index_id"] == "BRTI"
  assert ctx["index_live"] == 100062.0
  assert ctx["bet_side"] == "UP"
  assert ctx["reassess_supports_bet"] is True
  assert ctx["slot_monitor_action"] == "HOLD"
  detail = format_slot15_exit_context_detail(ctx)
  assert "BRTI $100,062.00" in detail
  assert "bet UP" in detail
  assert "reassess 62% UP" in detail
  assert "slot HOLD" in detail


def test_slot15_exit_trade_logs_exit_context_json():
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
      "entry_price_cents": 55,
      "cost_usd": 5.5,
      "signal": "LONG",
      "reference_price": 100000.0,
    })
    bot = Slot15Bot(store, asset="btc")
    tab = {
      "ok": True,
      "slot_key": slot_key,
      "monitor": {
        "signal_at_open": "LONG",
        "bet_side": "UP",
        "action": "HOLD",
        "reference_price": 100000.0,
        "current_price": 100050.0,
        "reassessed_prob_up": 0.60,
        "reassess_summary": "60% UP at close",
        "seconds_remaining": 300,
      },
      "kalshi": {
        "market_ticker": "KXBTC15M-TEST",
        "yes_bid": 0.51,
        "yes_ask": 0.51,
        "yes_mid": 0.51,
      },
    }
    actions = bot.run_continuous_cycle(tab)
    exits = [a for a in actions if a.get("action") == "exit"]
    assert len(exits) == 1
    assert "BRTI $100,050.00" in (exits[0].get("detail") or "")
    assert exits[0].get("exit_context") is not None
    assert exits[0]["exit_context"]["reassess_supports_bet"] is True
    assert exits[0]["exit_context"]["slot_monitor_action"] == "HOLD"
