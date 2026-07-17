"""Tests for manual 15m (slot) human trading."""

from __future__ import annotations

from pathlib import Path

from src.trading.human_slot15_trade import (
  execute_slot15_manual_enter,
  execute_slot15_manual_exit,
  preview_slot15_manual_entry,
  settle_expired_slot15_human_positions,
  side_from_slot15_signal,
  slot15_pick_from_tab,
)
from src.trading.human_trade_store import HumanTradeStore


def _tab(*, slot_key="2026-07-17T12:00:00+00:00", signal="LONG", yes_bid=0.48, yes_ask=0.52):
  return {
    "ok": True,
    "slot_key": slot_key,
    "paper_max_spread_cents": 40,
    "prediction": {"signal": signal, "prob_up": 0.62},
    "monitor": {"slot_start": slot_key, "current_price": 65000.0},
    "kalshi": {
      "market_ticker": "KXBTC15M-26JUL171200-15",
      "title": "BTC up",
      "yes_bid": yes_bid,
      "yes_ask": yes_ask,
      "yes_mid": (yes_bid + yes_ask) / 2,
    },
    "brti_live": 65010.0,
  }


def test_side_from_slot15_signal():
  assert side_from_slot15_signal("LONG") == "yes"
  assert side_from_slot15_signal("LATE SHORT") == "no"
  assert side_from_slot15_signal("NO TRADE") is None


def test_slot15_pick_from_tab():
  pick = slot15_pick_from_tab(_tab())
  assert pick["ticker"].startswith("KXBTC15M")
  assert pick["signal"] == "LONG"


def test_paper_enter_and_exit(tmp_path: Path):
  store = HumanTradeStore(tmp_path / "human15.db")
  cfg = {"human_trading": {"paper_bankroll_initial_usd": 100, "max_stake_per_entry_usd": 5}}
  tab = _tab()
  preview = preview_slot15_manual_entry(
    store=store,
    tab=tab,
    market_ticker=tab["kalshi"]["market_ticker"],
    side="yes",
    mode="paper",
    cfg=cfg,
    asset="btc",
  )
  assert preview["ok"] is True
  assert preview["fill_preview"]["ok"] is True

  entered = execute_slot15_manual_enter(
    store=store,
    tab=tab,
    market_ticker=tab["kalshi"]["market_ticker"],
    side="yes",
    mode="paper",
    cfg=cfg,
    asset="btc",
  )
  assert entered["ok"] is True
  pos = entered["position"]
  assert pos["side"] == "yes"
  assert int(pos["contracts"]) >= 1

  exited = execute_slot15_manual_exit(
    store=store,
    tab=_tab(yes_bid=0.60, yes_ask=0.64),
    position_id=pos["id"],
    cfg=cfg,
  )
  assert exited["ok"] is True
  assert exited["trade"]["action"] == "exit"
  assert store.open_positions() == []


def test_settle_prior_slot(tmp_path: Path, monkeypatch):
  store = HumanTradeStore(tmp_path / "human15b.db")
  cfg = {"human_trading": {"paper_bankroll_initial_usd": 100}}
  tab = _tab(slot_key="slot-current")

  # Current-slot open should not settle
  entered = execute_slot15_manual_enter(
    store=store,
    tab=tab,
    market_ticker=tab["kalshi"]["market_ticker"],
    side="yes",
    mode="paper",
    cfg=cfg,
    asset="btc",
  )
  assert entered["ok"]
  assert settle_expired_slot15_human_positions(
    store, current_slot_key="slot-current", tab=tab, cfg=cfg,
  ) == []

  # Prior-slot open settles when rollover says so
  store.open_position({
    "id": "old-leg",
    "event_ticker": "slot-old",
    "market_ticker": tab["kalshi"]["market_ticker"],
    "side": "yes",
    "contracts": 2,
    "entry_price_cents": 50,
    "cost_usd": 1.0,
    "signal": "LONG",
    "label": "BTC up",
    "mode": "paper",
  })
  monkeypatch.setattr(
    "src.trading.human_slot15_trade.should_rollover_close_slot15_leg",
    lambda pos, prev, **kw: str(pos.get("event_ticker")) == "slot-old",
  )
  monkeypatch.setattr(
    "src.trading.human_slot15_trade.resolve_slot15_rollover_exit_cents",
    lambda pos, **kw: (100, "test settle"),
  )
  settled = settle_expired_slot15_human_positions(
    store, current_slot_key="slot-current", tab=tab, cfg=cfg,
  )
  assert len(settled) == 1
  assert settled[0]["exit_price_cents"] == 100
  assert settled[0]["position_id"] == "old-leg"
  open_ids = {p["id"] for p in store.open_positions()}
  assert "old-leg" not in open_ids
  assert entered["position"]["id"] in open_ids
