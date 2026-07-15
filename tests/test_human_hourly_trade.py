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


def test_enrich_open_positions_kalshi_quote_override():
  from src.trading.human_hourly_trade import enrich_open_positions_fast_marks

  tab = _tab_with_pick()
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

  class _FakeKalshi:
    def get_market_ticker(self, ticker):
      assert ticker == "KXBTCD-26JUL1518-T64000"
      return {
        "yes_bid_dollars": "0.55",
        "yes_ask_dollars": "0.57",
        "title": "≥ $64,000",
        "strike_type": "greater",
        "floor_strike": 64000.0,
      }

  enriched = enrich_open_positions_fast_marks(
    open_pos,
    kalshi=_FakeKalshi(),
    tab=tab,
    cfg={},
  )
  assert enriched[0]["mark_price_cents"] == 55
  assert enriched[0]["quote_source"] == "kalshi_live"
  assert enriched[0]["unrealized_pnl_usd"] == 0.10


def test_settle_expired_human_paper_positions_pays_winners(tmp_path: Path):
  from src.trading.human_hourly_trade import settle_expired_human_positions
  from src.trading.hourly_event_time import hourly_event_settle_utc

  store = HumanTradeStore(tmp_path / "human.db")
  # Settled past hour (from hourly_event_time tests).
  prev = "KXBTCD-26JUN3005"
  settle_at = hourly_event_settle_utc(prev)
  assert settle_at is not None
  pos = store.open_position({
    "event_ticker": prev,
    "market_ticker": f"{prev}-T65000",
    "side": "no",
    "contracts": 3,
    "entry_price_cents": 71,
    "cost_usd": 2.13,
    "label": "$65,000 or above",
    "contract_type": "threshold",
    "strike_type": "greater",
    "floor_strike": 65000.0,
    "mode": "paper",
  })
  store.debit_paper_for_entry(2.13, 100.0)
  store.log_trade({
    "event_ticker": prev,
    "action": "enter",
    "mode": "paper",
    "market_ticker": f"{prev}-T65000",
    "side": "no",
    "contracts": 3,
    "price_cents": 71,
    "entry_price_cents": 71,
    "cost_usd": 2.13,
    "label": "$65,000 or above",
    "status": "filled",
    "position_id": pos["id"],
  })

  class _FakeKalshi:
    def get_market_ticker(self, ticker):
      return {"result": "no", "title": "$65,000 or above"}

  # Official Kalshi result (NO wins) — not late live tape.
  rows = settle_expired_human_positions(
    store,
    current_event_ticker="KXBTCD-26JUN3017",
    settle_price=66000.0,  # late tape would wrongly lose; must use Kalshi result
    cfg={"human_trading": {"paper_bankroll_initial_usd": 100}},
    index_id="BRTI",
    kalshi=_FakeKalshi(),
    asset="btc",
  )
  assert len(rows) == 1
  assert rows[0]["exit_price_cents"] == 100
  # (100-71)*3/100 = +0.87
  assert abs(float(rows[0]["pnl_usd"]) - 0.87) < 0.01
  assert store.open_positions() == []
  bank = store.reconcile_paper_bankroll(100.0)
  # 100 - 2.13 + 2.13 + 0.87 ≈ 100.87
  assert abs(bank["paper_bankroll_usd"] - 100.87) < 0.02


def test_repair_orphan_enter_returns_cash(tmp_path: Path):
  """Enter with no exit after hour end must still pay settlement (not leave a ghost enter)."""
  from src.trading.human_hourly_trade import settle_expired_human_positions
  from src.trading.hourly_event_time import hourly_event_settle_utc

  store = HumanTradeStore(tmp_path / "human.db")
  prev = "KXBTCD-26JUN3005"
  assert hourly_event_settle_utc(prev) is not None
  pid = "orphan-1"
  store.debit_paper_for_entry(2.40, 100.0)
  store.log_trade({
    "event_ticker": prev,
    "action": "enter",
    "mode": "paper",
    "market_ticker": f"{prev}-T66000",
    "side": "no",
    "contracts": 3,
    "price_cents": 80,
    "entry_price_cents": 80,
    "cost_usd": 2.40,
    "label": "$66,000 or above",
    "status": "filled",
    "position_id": pid,
    "entry_context": {
      "features": {
        "strike_type": "greater",
        "floor_strike": 66000.0,
        "contract_type": "threshold",
      },
    },
  })
  # Position vanished from open book (UI hour roll) but enter remains.
  assert store.open_positions() == []
  # Reconcile returns principal when open_cost=0; win is still missing until exit.
  mid = store.reconcile_paper_bankroll(100.0)
  assert abs(mid["paper_bankroll_usd"] - 100.0) < 0.02

  class _FakeKalshi:
    def get_market_ticker(self, ticker):
      return {"result": "no"}

  rows = settle_expired_human_positions(
    store,
    current_event_ticker="KXBTCD-26JUN3017",
    settle_price=None,
    cfg={"human_trading": {"paper_bankroll_initial_usd": 100}},
    kalshi=_FakeKalshi(),
    asset="btc",
  )
  assert len(rows) == 1
  assert rows[0]["exit_price_cents"] == 100
  bank = store.reconcile_paper_bankroll(100.0)
  # win (100-80)*3/100 = +0.60 → bankroll 100.60
  assert abs(bank["paper_bankroll_usd"] - 100.60) < 0.02
  exits = [t for t in store.list_trades(limit=10) if t["action"] == "exit"]
  assert exits and float(exits[0]["pnl_usd"]) == 0.60


def test_stuck_open_leg_gets_cash_and_win(tmp_path: Path):
  """Open leg hidden by hour filter — still open in DB — must unlock capital + pay win."""
  from src.trading.human_hourly_trade import settle_expired_human_positions
  from src.trading.hourly_event_time import hourly_event_settle_utc

  store = HumanTradeStore(tmp_path / "human.db")
  prev = "KXBTCD-26JUN3005"
  assert hourly_event_settle_utc(prev) is not None
  store.debit_paper_for_entry(2.13, 100.0)
  pos = store.open_position({
    "event_ticker": prev,
    "market_ticker": f"{prev}-T65000",
    "side": "no",
    "contracts": 3,
    "entry_price_cents": 71,
    "cost_usd": 2.13,
    "label": "$65,000 or above",
    "contract_type": "threshold",
    "strike_type": "greater",
    "floor_strike": 65000.0,
    "mode": "paper",
  })
  store.log_trade({
    "event_ticker": prev,
    "action": "enter",
    "mode": "paper",
    "market_ticker": f"{prev}-T65000",
    "side": "no",
    "contracts": 3,
    "price_cents": 71,
    "entry_price_cents": 71,
    "cost_usd": 2.13,
    "label": "$65,000 or above",
    "status": "filled",
    "position_id": pos["id"],
  })
  stuck = store.reconcile_paper_bankroll(100.0)
  assert abs(stuck["paper_bankroll_usd"] - 97.87) < 0.02  # capital locked in open

  class _FakeKalshi:
    def get_market_ticker(self, ticker):
      return {"result": "no"}

  settle_expired_human_positions(
    store,
    current_event_ticker="KXBTCD-26JUN3017",
    settle_price=67000.0,  # late tape must not mark NO as loss
    cfg={"human_trading": {"paper_bankroll_initial_usd": 100}},
    kalshi=_FakeKalshi(),
    asset="btc",
  )
  bank = store.reconcile_paper_bankroll(100.0)
  assert store.open_positions() == []
  assert abs(bank["paper_bankroll_usd"] - 100.87) < 0.02


def test_hour_rolled_settles_even_if_settle_clock_unparsed(tmp_path: Path, monkeypatch):
  """Prior-hour opens must unlock bankroll whenever a new hour is live."""
  from src.trading import human_hourly_trade as hht

  store = HumanTradeStore(tmp_path / "human.db")
  store.debit_paper_for_entry(5.0, 100.0)
  pos = store.open_position({
    "event_ticker": "KXBTCD-26JUL1516",
    "market_ticker": "KXBTCD-26JUL1516-T65000",
    "side": "no",
    "contracts": 5,
    "entry_price_cents": 100,
    "cost_usd": 5.0,
    "label": "$65,000 or above",
    "contract_type": "threshold",
    "strike_type": "greater",
    "floor_strike": 65000.0,
    "mode": "paper",
  })
  # Pretend settle-time check fails (would previously leave capital locked forever).
  monkeypatch.setattr(
    "src.trading.hourly_event_time.hourly_event_has_settled",
    lambda *_a, **_k: False,
  )
  monkeypatch.setattr(
    "src.trading.hourly_event_time.hourly_event_settle_utc",
    lambda *_a, **_k: None,
  )

  class _FakeKalshi:
    def get_market_ticker(self, ticker):
      return {"result": "no"}

  stuck = store.reconcile_paper_bankroll(100.0)
  assert abs(stuck["paper_bankroll_usd"] - 95.0) < 0.02
  rows = hht.settle_expired_human_positions(
    store,
    current_event_ticker="KXBTCD-26JUL1517",
    settle_price=None,
    cfg={"human_trading": {"paper_bankroll_initial_usd": 100}},
    kalshi=_FakeKalshi(),
    asset="btc",
  )
  assert len(rows) == 1
  assert rows[0]["exit_price_cents"] == 100
  bank = store.reconcile_paper_bankroll(100.0)
  assert store.open_positions() == []
  # entry was 100¢ so win pnl = 0, capital returned → $100
  assert abs(bank["paper_bankroll_usd"] - 100.0) < 0.02
  assert pos["id"]


def test_repair_enter_without_position_id(tmp_path: Path):
  """Unpaid enters must settle even when position_id was never stored."""
  from src.trading.human_hourly_trade import settle_expired_human_positions
  from src.trading.hourly_event_time import hourly_event_settle_utc

  store = HumanTradeStore(tmp_path / "human.db")
  prev = "KXBTCD-26JUN3005"
  assert hourly_event_settle_utc(prev) is not None
  store.debit_paper_for_entry(2.40, 100.0)
  store.log_trade({
    "event_ticker": prev,
    "action": "enter",
    "mode": "paper",
    "market_ticker": f"{prev}-T66000",
    "side": "no",
    "contracts": 3,
    "price_cents": 80,
    "entry_price_cents": 80,
    "cost_usd": 2.40,
    "label": "$66,000 or above",
    "status": "filled",
    # no position_id — older bug path
  })

  class _FakeKalshi:
    def get_market_ticker(self, ticker):
      return {"result": "no"}

  rows = settle_expired_human_positions(
    store,
    current_event_ticker="KXBTCD-26JUN3017",
    settle_price=None,
    cfg={"human_trading": {"paper_bankroll_initial_usd": 100}},
    kalshi=_FakeKalshi(),
    asset="btc",
  )
  assert len(rows) == 1
  assert rows[0]["exit_price_cents"] == 100
  bank = store.reconcile_paper_bankroll(100.0)
  assert abs(bank["paper_bankroll_usd"] - 100.60) < 0.02


def test_upgrade_cashback_scratch_to_kalshi_win(tmp_path: Path):
  """Cash-back +$0 settles must upgrade to 100¢ when expiration_value is known."""
  from src.trading.human_hourly_trade import settle_expired_human_positions
  from src.trading.hourly_event_time import hourly_event_settle_utc

  store = HumanTradeStore(tmp_path / "human.db")
  prev = "KXBTCD-26JUN3005"
  assert hourly_event_settle_utc(prev) is not None
  store.debit_paper_for_entry(2.40, 100.0)
  store.log_trade({
    "event_ticker": prev,
    "action": "enter",
    "mode": "paper",
    "market_ticker": f"{prev}-T66000",
    "side": "no",
    "contracts": 3,
    "price_cents": 80,
    "entry_price_cents": 80,
    "cost_usd": 2.40,
    "label": "$66,000 or above",
    "status": "filled",
    "position_id": "scratch-1",
  })
  # Prior cash-back settle (what 5.0.125 wrote when result was missing).
  store.log_trade({
    "event_ticker": prev,
    "action": "exit",
    "mode": "paper",
    "market_ticker": f"{prev}-T66000",
    "side": "no",
    "contracts": 3,
    "price_cents": 80,
    "entry_price_cents": 80,
    "exit_price_cents": 80,
    "cost_usd": 2.40,
    "pnl_usd": 0.0,
    "label": "$66,000 or above",
    "status": "filled",
    "position_id": "scratch-1",
    "detail": "PAPER EXIT (HOUR SETTLEMENT): NO ×3 @ 80¢ (entry 80¢) — cash-back @ entry 80¢",
  })
  store.apply_paper_exit_settlement(2.40, 0.0, 100.0)

  class _FakeKalshi:
    def get_market_ticker(self, ticker):
      return {"expiration_value": "64958.13", "status": "finalized"}

  rows = settle_expired_human_positions(
    store,
    current_event_ticker="KXBTCD-26JUN3017",
    settle_price=None,
    cfg={"human_trading": {"paper_bankroll_initial_usd": 100}},
    kalshi=_FakeKalshi(),
    asset="btc",
  )
  assert rows
  assert rows[0]["exit_price_cents"] == 100
  assert abs(float(rows[0]["pnl_usd"]) - 0.60) < 0.01
  bank = store.reconcile_paper_bankroll(100.0)
  assert abs(bank["paper_bankroll_usd"] - 100.60) < 0.02
  summary = store.pnl_summary(mode="paper")
  assert summary["wins"] == 1
  assert summary["pushes"] == 0
  assert summary["win_rate"] == 1.0


def test_win_rate_excludes_scratch_pushes(tmp_path: Path):
  store = HumanTradeStore(tmp_path / "human.db")
  for pnl in (0.5, 0.3, -0.2, 0.0, 0.0):
    store.log_trade({
      "event_ticker": "KXBTCD-26JUN3005",
      "action": "exit",
      "mode": "paper",
      "market_ticker": "KXBTCD-26JUN3005-T64000",
      "side": "yes",
      "contracts": 1,
      "entry_price_cents": 50,
      "exit_price_cents": 50 if pnl == 0 else (100 if pnl > 0 else 0),
      "pnl_usd": pnl,
      "status": "filled",
    })
  s = store.pnl_summary(mode="paper")
  assert s["wins"] == 2
  assert s["losses"] == 1
  assert s["pushes"] == 2
  assert s["closed_legs"] == 5
  # 2/(2+1) = 0.667 — not 2/5
  assert abs(s["win_rate"] - 0.667) < 0.001


def test_settle_expired_skips_current_hour(tmp_path: Path):
  from src.trading.human_hourly_trade import settle_expired_human_positions

  store = HumanTradeStore(tmp_path / "human.db")
  cur = "KXBTCD-26JUL1518"
  store.open_position({
    "event_ticker": cur,
    "market_ticker": f"{cur}-T64000",
    "side": "yes",
    "contracts": 2,
    "entry_price_cents": 50,
    "cost_usd": 1.0,
    "label": "$64,000 or above",
    "strike_type": "greater",
    "floor_strike": 64000.0,
    "mode": "paper",
  })
  rows = settle_expired_human_positions(
    store,
    current_event_ticker=cur,
    settle_price=64100.0,
    cfg={},
  )
  assert rows == []
  assert len(store.open_positions(cur)) == 1


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
    "position_id": "pos-1",
    "status": "filled",
    "created_at": "2026-07-15T18:00:00+00:00",
    "entry_context": {
      "features": {"spot_price": 64000, "hours_to_settle": 0.4, "edge": 0.12},
      "bot_counterfactual": {"would_enter": False, "skip_reasons": ["budget"]},
    },
  })
  store.log_trade({
    "event_ticker": "KXBTCD-26JUL1518",
    "action": "exit",
    "mode": "paper",
    "market_ticker": "T1",
    "side": "yes",
    "contracts": 1,
    "price_cents": 40,
    "entry_price_cents": 30,
    "exit_price_cents": 40,
    "pnl_usd": 0.10,
    "position_id": "pos-1",
    "status": "filled",
    "created_at": "2026-07-15T18:10:00+00:00",
    "entry_context": {
      "features": {"spot_price": 64100},
      "exit_reason": "manual_dashboard",
      "bot_exit_signal": {"alert": "TAKE PROFIT"},
    },
  })
  rows = export_human_training_rows(store)
  assert len(rows) == 1
  assert rows[0]["bot_would_enter"] is False
  assert rows[0]["closed"] is True
  assert rows[0]["closed_pnl_usd"] == 0.10
  assert rows[0]["spot_price"] == 64000
  assert rows[0]["bot_exit_signal"]["alert"] == "TAKE PROFIT"
