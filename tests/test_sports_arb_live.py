"""Tests for sports live cover / value execution helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from src.trading.sports_arb_bot import SportsArbBot, _ask_cents, _fingerprint
from src.trading.sports_arb_store import SportsArbSettings, SportsArbStore


def test_ask_cents_rounds_up():
  assert _ask_cents(0.401) == 41
  assert _ask_cents(0.40) == 40


def test_fingerprint():
  assert _fingerprint({"event_ticker": "E1", "kind": "binary_yes_no"}) == "E1|binary_yes_no"


def test_live_execute_places_both_legs(tmp_path: Path):
  store = SportsArbStore(tmp_path / "s.db")
  store.save_settings(SportsArbSettings(enabled=True, dutch_live=True, dutch_max_stake_usd=5, dutch_max_open_usd=25))
  kalshi = MagicMock()
  kalshi.authenticated = True
  kalshi.create_order.side_effect = [
    {"order": {"order_id": "o1", "status": "executed", "fill_count": 2}},
    {"order": {"order_id": "o2", "status": "executed", "fill_count": 2}},
  ]
  cfg = {
    "sports": {
      "enabled": True,
      "bot": {"allow_live": True, "max_live_per_scan": 0, "max_live_trades_per_day": 0},
      "strategies": {"dutch_same": {"enabled": True}},
    },
    "paths": {"logs": str(tmp_path)},
  }
  bot = SportsArbBot(cfg, store, kalshi=kalshi)
  opp = {
    "strategy": "dutch_same",
    "kind": "binary_yes_no",
    "event_ticker": "EVT1",
    "edge_usd": 0.12,
    "total_cost_usd": 1.8,
    "legs": [
      {"ticker": "M1", "side": "yes", "ask": 0.40, "contracts": 2},
      {"ticker": "M1", "side": "no", "ask": 0.50, "contracts": 2},
    ],
  }
  result = bot._execute_live_cover(opp)
  assert result["ok"] is True
  assert kalshi.create_order.call_count == 2
  trades = store.list_trades()
  assert trades and trades[0]["status"] == "live_filled"


def test_live_value_places_one_leg(tmp_path: Path):
  store = SportsArbStore(tmp_path / "s.db")
  store.save_settings(SportsArbSettings(enabled=True, value_live=True, value_max_stake_usd=5, value_max_open_usd=25))
  kalshi = MagicMock()
  kalshi.authenticated = True
  kalshi.create_order.return_value = {
    "order": {"order_id": "ov1", "status": "executed", "fill_count": 5},
  }
  cfg = {
    "sports": {
      "enabled": True,
      "bot": {"allow_live": True},
      "strategies": {
        "dutch_same": {"enabled": False},
        "value_sharp": {"enabled": True, "allow_kalshi_live": True},
      },
    },
    "paths": {"logs": str(tmp_path)},
  }
  bot = SportsArbBot(cfg, store, kalshi=kalshi)
  opp = {
    "strategy": "value_sharp",
    "kind": "kalshi_value",
    "venue": "kalshi",
    "event_ticker": "EVT-V",
    "selection": "Argentina",
    "edge_usd": 0.4,
    "edge_prob": 0.08,
    "total_cost_usd": 2.0,
    "venue_ask": 0.40,
    "contracts": 5,
    "legs": [{"ticker": "KX-ARG", "side": "yes", "ask": 0.40, "contracts": 5}],
  }
  result = bot._execute_live_value(opp)
  assert result["ok"] is True
  assert kalshi.create_order.call_count == 1
  assert store.list_trades()[0]["status"] == "live_filled"


def test_act_routes_value_only_when_value_live(tmp_path: Path):
  store = SportsArbStore(tmp_path / "s.db")
  store.save_settings(SportsArbSettings(
    enabled=True,
    dutch_live=False,
    value_live=True,
    value_max_open_usd=40,
    value_max_stake_usd=5,
  ))
  kalshi = MagicMock()
  kalshi.authenticated = True
  kalshi.create_order.return_value = {
    "order": {"order_id": "ov2", "status": "executed", "fill_count": 3},
  }
  cfg = {
    "sports": {
      "enabled": True,
      "bot": {"allow_live": True},
      "strategies": {"value_sharp": {"enabled": True, "allow_kalshi_live": True}},
    },
    "paths": {"logs": str(tmp_path)},
  }
  bot = SportsArbBot(cfg, store, kalshi=kalshi)
  actions = bot._act_on_opportunities(
    [{
      "strategy": "value_sharp",
      "kind": "kalshi_value",
      "venue": "kalshi",
      "event_ticker": "EVT-V2",
      "selection": "England",
      "edge_usd": 0.3,
      "total_cost_usd": 1.2,
      "legs": [{"ticker": "KX-ENG", "side": "yes", "ask": 0.37, "contracts": 3}],
    }],
    store.get_settings(),
    {"allow_kalshi_live": True},
  )
  assert any(a.get("ok") for a in actions)
  assert kalshi.create_order.called


def test_separate_budgets_block_per_strategy(tmp_path: Path):
  store = SportsArbStore(tmp_path / "s.db")
  store.save_settings(SportsArbSettings(
    enabled=True,
    dutch_live=True,
    value_live=True,
    dutch_max_open_usd=2.0,
    value_max_open_usd=50.0,
    dutch_max_stake_usd=5,
    value_max_stake_usd=5,
  ))
  # Pretend dutch already spent $1.5 today
  store.log_trade(
    {"strategy": "dutch_same", "event_ticker": "E0", "kind": "binary_yes_no", "edge_usd": 0.1, "total_cost_usd": 1.5},
    mode="live",
    status="live_filled",
  )
  kalshi = MagicMock()
  kalshi.authenticated = True
  cfg = {
    "sports": {
      "enabled": True,
      "bot": {"allow_live": True},
      "strategies": {
        "dutch_same": {"enabled": True},
        "value_sharp": {"enabled": True, "allow_kalshi_live": True},
      },
    },
    "paths": {"logs": str(tmp_path)},
  }
  bot = SportsArbBot(cfg, store, kalshi=kalshi)
  actions = bot._act_on_opportunities(
    [{
      "strategy": "dutch_same",
      "kind": "binary_yes_no",
      "event_ticker": "E-DUTCH",
      "edge_usd": 0.2,
      "total_cost_usd": 1.0,  # would exceed dutch budget 1.5+1.0 > 2.0
      "legs": [
        {"ticker": "A", "side": "yes", "ask": 0.4, "contracts": 1},
        {"ticker": "A", "side": "no", "ask": 0.5, "contracts": 1},
      ],
    }],
    store.get_settings(),
    {"allow_kalshi_live": True},
  )
  assert any(a.get("reason") == "dutch_same_max_open_usd" for a in actions)
  assert not kalshi.create_order.called


def test_paper_signal_does_not_block_live_value(tmp_path: Path):
  store = SportsArbStore(tmp_path / "s.db")
  store.save_settings(SportsArbSettings(
    enabled=True, value_live=True, value_max_stake_usd=5, value_max_open_usd=40,
  ))
  opp = {
    "strategy": "value_sharp",
    "kind": "kalshi_value",
    "venue": "kalshi",
    "event_ticker": "EVT-V3",
    "selection": "Norway",
    "edge_usd": 0.5,
    "total_cost_usd": 2.0,
    "legs": [{"ticker": "KX-NOR", "side": "yes", "ask": 0.39, "contracts": 5}],
  }
  store.log_trade(opp, mode="paper", status="paper_signal")
  kalshi = MagicMock()
  kalshi.authenticated = True
  kalshi.create_order.return_value = {
    "order": {"order_id": "ov3", "status": "executed", "fill_count": 5},
  }
  cfg = {
    "sports": {
      "enabled": True,
      "bot": {"allow_live": True},
      "strategies": {"value_sharp": {"enabled": True, "allow_kalshi_live": True}},
    },
    "paths": {"logs": str(tmp_path)},
  }
  bot = SportsArbBot(cfg, store, kalshi=kalshi)
  actions = bot._act_on_opportunities([opp], store.get_settings(), {"allow_kalshi_live": True})
  assert any(a.get("ok") for a in actions), actions
  assert kalshi.create_order.called


def test_strong_bets_only_skips_non_strong_value(tmp_path: Path):
  store = SportsArbStore(tmp_path / "s.db")
  store.save_settings(SportsArbSettings(
    enabled=True,
    value_live=True,
    value_strong_bets_only=True,
    value_max_open_usd=40,
    value_max_stake_usd=5,
  ))
  kalshi = MagicMock()
  kalshi.authenticated = True
  cfg = {
    "sports": {
      "enabled": True,
      "bot": {"allow_live": True},
      "strategies": {"value_sharp": {"enabled": True, "allow_kalshi_live": True}},
    },
    "paths": {"logs": str(tmp_path)},
  }
  bot = SportsArbBot(cfg, store, kalshi=kalshi)
  moderate = {
    "strategy": "value_sharp",
    "kind": "kalshi_value",
    "venue": "kalshi",
    "event_ticker": "EVT-MOD",
    "edge_usd": 0.2,
    "total_cost_usd": 1.0,
    "legs": [{"ticker": "KX-MOD", "side": "yes", "ask": 0.35, "contracts": 2}],
    "bet_assessment": {"actionable_bet": True, "edge_tier": "MODERATE"},
  }
  actions = bot._act_on_opportunities([moderate], store.get_settings(), {"allow_kalshi_live": True})
  assert not kalshi.create_order.called
  assert not store.list_trades()

  strong = {
    **moderate,
    "event_ticker": "EVT-STR",
    "legs": [{"ticker": "KX-STR", "side": "yes", "ask": 0.35, "contracts": 2}],
    "bet_assessment": {"actionable_bet": True, "edge_tier": "STRONG"},
  }
  kalshi.create_order.return_value = {
    "order": {"order_id": "ov4", "status": "executed", "fill_count": 2},
  }
  actions = bot._act_on_opportunities([strong], store.get_settings(), {"allow_kalshi_live": True})
  assert kalshi.create_order.called
  assert any(a.get("ok") for a in actions)


def test_live_failed_fingerprint_blocks_retry(tmp_path: Path):
  store = SportsArbStore(tmp_path / "s.db")
  store.save_settings(SportsArbSettings(
    enabled=True,
    value_live=True,
    value_max_open_usd=40,
    value_max_stake_usd=5,
  ))
  opp = {
    "strategy": "value_sharp",
    "kind": "kalshi_value",
    "venue": "kalshi",
    "event_ticker": "KXMLBGAME-26JUL121610TORSD",
    "selection": "TOR",
    "edge_usd": 0.52,
    "total_cost_usd": 1.93,
    "legs": [{"ticker": "KX-TOR", "side": "yes", "ask": 0.39, "contracts": 5}],
  }
  store.log_trade(
    opp,
    mode="live",
    status="live_failed",
    extra={"error": "409 Client Error: Conflict"},
  )
  kalshi = MagicMock()
  kalshi.authenticated = True
  cfg = {
    "sports": {
      "enabled": True,
      "bot": {"allow_live": True},
      "strategies": {"value_sharp": {"enabled": True, "allow_kalshi_live": True}},
    },
    "paths": {"logs": str(tmp_path)},
  }
  bot = SportsArbBot(cfg, store, kalshi=kalshi)
  actions = bot._act_on_opportunities([opp], store.get_settings(), {"allow_kalshi_live": True})
  assert not kalshi.create_order.called
  assert any(a.get("reason") == "already_seen" for a in actions)


def test_live_value_skips_when_already_positioned(tmp_path: Path):
  store = SportsArbStore(tmp_path / "s.db")
  kalshi = MagicMock()
  kalshi.authenticated = True
  kalshi.get_market_position.return_value = 5.0
  cfg = {"sports": {"bot": {"allow_live": True}}, "paths": {"logs": str(tmp_path)}}
  bot = SportsArbBot(cfg, store, kalshi=kalshi)
  opp = {
    "strategy": "value_sharp",
    "kind": "kalshi_value",
    "venue": "kalshi",
    "event_ticker": "EVT-POS",
    "selection": "HOME",
    "edge_usd": 0.4,
    "total_cost_usd": 1.5,
    "legs": [{"ticker": "KX-HOME", "side": "yes", "ask": 0.30, "contracts": 5}],
  }
  result = bot._execute_live_value(opp)
  assert result["action"] == "live_skip"
  assert result["reason"] == "already_positioned"
  assert not kalshi.create_order.called


def test_live_execute_cancels_on_second_leg_fail(tmp_path: Path):
  store = SportsArbStore(tmp_path / "s.db")
  kalshi = MagicMock()
  kalshi.authenticated = True
  kalshi.create_order.side_effect = [
    {"order": {"order_id": "o1", "status": "executed", "fill_count": 1}},
    {"order": {"order_id": "o2", "status": "canceled", "fill_count": 0}},
  ]
  cfg = {"sports": {"bot": {"allow_live": True}}, "paths": {"logs": str(tmp_path)}}
  bot = SportsArbBot(cfg, store, kalshi=kalshi)
  opp = {
    "event_ticker": "EVT1",
    "kind": "binary_yes_no",
    "edge_usd": 0.1,
    "total_cost_usd": 0.9,
    "legs": [
      {"ticker": "M1", "side": "yes", "ask": 0.40, "contracts": 1},
      {"ticker": "M1", "side": "no", "ask": 0.50, "contracts": 1},
    ],
  }
  result = bot._execute_live_cover(opp)
  assert result["ok"] is False
  assert kalshi.cancel_order.called or kalshi.cancel_resting_orders_for_ticker.called
  assert store.list_trades()[0]["status"] == "live_failed"
