"""Tests for dashboard manual (human) hourly trading lane."""

from __future__ import annotations

from pathlib import Path

from src.trading.human_bot_compare import build_human_bot_compare, export_human_training_rows
from src.trading.human_hourly_trade import (
  build_bot_counterfactual,
  pick_from_tab,
  preview_manual_entry,
)
from src.trading.human_trade_store import HumanTradeStore
from src.trading.hourly_bot_store import HourlyBotStore


def _tab_with_pick() -> dict:
  return {
    "ok": True,
    "event": {"event_ticker": "KXBTCD-26JUL1518"},
    "live": {
      "current_price": 64000.0,
      "hours_to_settle": 0.45,
      "terminal_sigma": 180.0,
      "index_id": "BRTI",
      "regime": {"allow_trade": True, "reasons": []},
      "strategy_threshold": {
        "contracts": [{
          "ticker": "KXBTCD-26JUL1518-T64000",
          "label": "≥ $64,000",
          "signal": "BUY YES",
          "edge": 0.18,
          "model_prob": 0.62,
          "kalshi_mid": 0.44,
          "yes_bid": 43,
          "yes_ask": 45,
          "strike_type": "greater",
          "floor_strike": 64000.0,
          "contract_type": "threshold",
        }],
      },
      "strategy_range": {"contracts": []},
    },
  }


def test_pick_from_tab_finds_threshold_contract():
  tab = _tab_with_pick()
  pick = pick_from_tab(tab, "KXBTCD-26JUL1518-T64000")
  assert pick and pick["signal"] == "BUY YES"


def test_preview_manual_entry_paper_ok(tmp_path: Path):
  store = HumanTradeStore(tmp_path / "human.db")
  tab = _tab_with_pick()
  out = preview_manual_entry(
    store=store,
    tab=tab,
    market_ticker="KXBTCD-26JUL1518-T64000",
    side="yes",
    mode="paper",
    bot_status={"last_skip_reason": "budget", "open_positions": []},
    cfg={"human_trading": {"paper_bankroll_initial_usd": 100}},
    asset="btc",
  )
  assert out["ok"] is True
  assert out["fill_preview"]["ok"] is True
  assert out["bot_counterfactual"]["would_enter"] is True


def test_execute_manual_enter_paper_round_trip(tmp_path: Path):
  from src.trading.human_hourly_trade import execute_manual_enter, execute_manual_exit

  store = HumanTradeStore(tmp_path / "human.db")
  tab = _tab_with_pick()
  entered = execute_manual_enter(
    store=store,
    tab=tab,
    market_ticker="KXBTCD-26JUL1518-T64000",
    side="yes",
    mode="paper",
    bot_status={"open_positions": []},
    cfg={"human_trading": {"paper_bankroll_initial_usd": 100}},
    asset="btc",
  )
  assert entered["ok"] is True
  pos_id = entered["position"]["id"]
  exited = execute_manual_exit(
    store=store,
    tab=tab,
    position_id=pos_id,
    cfg={"human_trading": {"paper_bankroll_initial_usd": 100}},
  )
  assert exited["ok"] is True
  assert exited["pnl_usd"] is not None
  trades = store.list_trades(limit=10)
  assert len(trades) == 2
  enter_trade = next(t for t in trades if t.get("action") == "enter")
  exit_trade = next(t for t in trades if t.get("action") == "exit")
  assert enter_trade["entry_context"]["bot_counterfactual"]["would_enter"] is True
  assert exit_trade["pnl_usd"] == exited["pnl_usd"]
  summary = store.pnl_summary(mode="paper")
  assert summary["closed_legs"] == 1
  assert summary["realized_pnl_usd"] == exited["pnl_usd"]
  # Bankroll must restore entry cost + apply P&L (not P&L alone).
  paper = store.get_paper_state_dict(100.0)
  assert abs(paper["paper_bankroll_usd"] - (100.0 + exited["pnl_usd"])) < 0.02
  status = store.status("KXBTCD-26JUL1518")
  assert status["paper_pnl"]["closed_legs"] == 1
  assert any(t.get("action") == "exit" for t in status["paper_recent_trades"])


def test_reconcile_paper_bankroll_restores_stuck_principal(tmp_path: Path):
  store = HumanTradeStore(tmp_path / "human.db")
  # Simulate bug: cost debited, exit only credited pnl.
  assert store.debit_paper_for_entry(10.0, 100.0)
  store.log_trade({
    "event_ticker": "KXBTCD-26JUL1518",
    "action": "exit",
    "mode": "paper",
    "market_ticker": "T1",
    "side": "yes",
    "contracts": 2,
    "price_cents": 50,
    "entry_price_cents": 40,
    "exit_price_cents": 50,
    "cost_usd": 0.8,
    "pnl_usd": 1.68,
    "status": "filled",
  })
  # Old buggy exit credit of pnl only (leaves principal stuck).
  store.apply_paper_exit_settlement(0.0, 1.68, 100.0)
  mid = store.get_paper_state_dict(100.0)
  assert mid["paper_bankroll_usd"] < 95  # still missing principal
  healed = store.reconcile_paper_bankroll(100.0)
  assert abs(healed["paper_bankroll_usd"] - 101.68) < 0.02
  assert healed["reconciled"] is True


def test_enrich_open_positions_marks_unrealized():
  from src.trading.human_hourly_trade import enrich_open_positions_marks

  tab = _tab_with_pick()
  # yes_bid 43 → mark 43¢ vs entry 50¢ × 2 = −$0.14
  open_pos = [{
    "id": "p1",
    "market_ticker": "KXBTCD-26JUL1518-T64000",
    "side": "yes",
    "contracts": 2,
    "entry_price_cents": 50,
    "signal": "BUY YES",
    "strike_type": "greater",
    "floor_strike": 64000.0,
  }]
  enriched = enrich_open_positions_marks(open_pos, tab)
  assert enriched[0]["mark_price_cents"] == 43
  assert enriched[0]["unrealized_pnl_usd"] == -0.14
  assert enriched[0]["bot_exit_signal"]["alert"] in ("HOLD", "CUT LOSSES", "TAKE PROFIT")


def test_enrich_open_positions_bot_cut_when_spot_against():
  from src.trading.human_hourly_trade import enrich_open_positions_marks

  tab = _tab_with_pick()
  tab["live"]["current_price"] = 63500.0  # below ≥64000 floor → spot against YES
  open_pos = [{
    "id": "p1",
    "market_ticker": "KXBTCD-26JUL1518-T64000",
    "side": "yes",
    "contracts": 2,
    "entry_price_cents": 50,
    "signal": "BUY YES",
    "strike_type": "greater",
    "contract_type": "threshold",
    "floor_strike": 64000.0,
  }]
  enriched = enrich_open_positions_marks(open_pos, tab, cfg={})
  assert enriched[0]["bot_exit_signal"]["alert"] == "CUT LOSSES"


def test_build_bot_counterfactual_blocks_range_spot_below_floor():
  pick = {
    "ticker": "KXBTC-26JUL1518-B63625",
    "signal": "BUY YES",
    "strike_type": "between",
    "contract_type": "range",
    "floor_strike": 63500.0,
    "cap_strike": 63749.99,
  }
  tab = {"live": {"current_price": 63200.0, "terminal_sigma": 180.0}}
  cf = build_bot_counterfactual(
    pick=pick,
    side="yes",
    tab=tab,
    bot_status={},
    cfg={"hourly": {"bot": {"live_inventory": {"range_band_spot_entry_guard": {"enabled": True}}}}},
    asset="btc",
  )
  assert cf["would_enter"] is False
  assert any(str(r).startswith("range_band_spot_below_floor") for r in cf["skip_reasons"])


def test_human_bot_compare_pairs_entries(tmp_path: Path):
  human = HumanTradeStore(tmp_path / "human.db")
  bot = HourlyBotStore(tmp_path / "bot.db")
  human.log_trade({
    "event_ticker": "KXBTCD-26JUL1518",
    "action": "enter",
    "mode": "paper",
    "market_ticker": "T1",
    "side": "yes",
    "contracts": 2,
    "price_cents": 40,
    "entry_price_cents": 40,
    "cost_usd": 0.8,
    "status": "filled",
    "created_at": "2026-07-15T18:00:00+00:00",
  })
  bot.log_trade({
    "event_ticker": "KXBTCD-26JUL1518",
    "trigger": "continuous",
    "action": "enter",
    "mode": "live",
    "market_ticker": "T1",
    "side": "yes",
    "contracts": 2,
    "price_cents": 41,
    "entry_price_cents": 41,
    "cost_usd": 0.82,
    "status": "filled",
    "created_at": "2026-07-15T18:01:00+00:00",
  })
  out = build_human_bot_compare(human, bot, asset="btc", bot_kind="hourly")
  assert out["ok"] is True
  assert out["pairing"]["paired_count"] == 1


def test_export_human_training_rows(tmp_path: Path):
  store = HumanTradeStore(tmp_path / "human.db")
  store.log_trade({
    "event_ticker": "KXBTCD-26JUL1518",
    "action": "enter",
    "mode": "paper",
    "market_ticker": "T1",
    "side": "yes",
    "contracts": 1,
    "price_cents": 30,
    "entry_price_cents": 30,
    "cost_usd": 0.3,
    "status": "filled",
    "entry_context": {
      "features": {"spot_price": 64000},
      "bot_counterfactual": {"would_enter": False},
    },
  })
  rows = export_human_training_rows(store)
  assert len(rows) == 1
  assert rows[0]["bot_would_enter"] is False
