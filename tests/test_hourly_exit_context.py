"""Tests for hourly exit context logging (ERTI/BRTI vet fields)."""

from __future__ import annotations

import tempfile
from pathlib import Path

from src.trading.hourly_bot import HourlyBot
from src.trading.hourly_bot_store import HourlyBotSettings, HourlyBotStore
from src.trading.hourly_exit_context import (
  build_hourly_exit_context,
  format_hourly_exit_context_detail,
)


def test_build_hourly_exit_context_includes_index_and_thesis():
  pos = {
    "side": "no",
    "signal": "BUY NO",
    "reference_price": 1615.0,
    "market_ticker": "KXETH-T1",
    "entry_edge": 0.12,
  }
  pick = {
    "signal": "BUY NO",
    "edge": 0.10,
    "contract_type": "range",
    "strike_type": "between",
    "floor_strike": 1610.0,
    "cap_strike": 1629.99,
  }
  tab = {
    "brti_live": 1629.0,
    "live": {
      "index_id": "ERTI",
      "current_price": 1629.0,
      "regime": {"allow_trade": True, "reasons": []},
    },
  }
  ctx = build_hourly_exit_context(
    pos=pos,
    pick=pick,
    tab=tab,
    live_price=1629.0,
    unrealized_pnl_usd=-0.45,
    exit_reason="CHEAP LEG CUT LOSS",
    position_alert={"alert": "HOLD", "detail": "Signal still supports"},
    bot_kind="hourly",
    hours_to_settle=0.25,
  )
  assert ctx["index_id"] == "ERTI"
  assert ctx["index_live"] == 1629.0
  assert ctx["contract_label"] == "$1,610.00–$1,629.99"
  assert ctx["signal_favors_held_side"] is True
  assert ctx["thesis_broken"] is False
  detail = format_hourly_exit_context_detail(ctx)
  assert "ERTI $1,629.00" in detail
  assert "signal supports" in detail
  assert "BUY NO" in detail


def test_hourly_exit_trade_logs_exit_context_json():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "hourly_bot_eth.db")
    store.save_settings(HourlyBotSettings(enabled=True, max_spend_per_hour_usd=25.0))
    store.open_position({
      "id": "p1",
      "event_ticker": "KXETH-1H",
      "market_ticker": "KXETH-T1",
      "side": "no",
      "contracts": 15,
      "entry_price_cents": 14,
      "cost_usd": 2.1,
      "signal": "BUY NO",
      "entry_edge": 0.12,
      "reference_price": 1615.0,
      "contract_type": "range",
      "floor_strike": 1610.0,
      "cap_strike": 1629.99,
    })
    bot = HourlyBot(store, asset="eth", kind="hourly")
    tab = {
      "ok": True,
      "event": {"event_ticker": "KXETH-1H"},
      "brti_live": 1629.0,
      "live": {
        "index_id": "ERTI",
        "current_price": 1629.0,
        "hours_to_settle": 0.4,
        "regime": {"allow_trade": True, "reasons": []},
        "primary_pick": {
          "ticker": "KXETH-T1",
          "signal": "BUY YES",
          "edge": 0.08,
          "contract_type": "range",
          "strike_type": "between",
          "floor_strike": 1610.0,
          "cap_strike": 1629.99,
          "yes_bid": 0.90,
          "yes_ask": 0.91,
          "no_bid": 0.09,
          "no_ask": 0.10,
        },
      },
      "locked": {},
    }
    cfg = {"hourly": {"bot": {"cheap_leg_max_entry_cents": 20, "cheap_leg_cut_loss_cents": 10}, "regime": {}}}
    actions = bot.run_continuous_cycle(tab, cfg=cfg)
    assert len(actions) == 1
    assert actions[0]["action"] == "exit"
    assert "ERTI $1,629.00" in (actions[0].get("detail") or "")
    assert actions[0].get("exit_context") is not None
    assert actions[0]["exit_context"]["index_id"] == "ERTI"
    assert actions[0]["exit_context"]["index_live"] == 1629.0
