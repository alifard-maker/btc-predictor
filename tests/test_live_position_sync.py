"""Tests for Kalshi inventory sync and live exit hygiene."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.trading.hourly_bot_store import HourlyBotStore
from src.trading.live_position_sync import (
  adopt_filled_resting_enters,
  adopt_kalshi_orphan_inventory,
  cancel_orphan_live_sell_orders,
  cancel_resting_enter_orders_for_hourly_event,
  confirm_kalshi_exit_fill,
  effective_kalshi_inventory,
  hourly_event_market_tickers_from_tab,
  kalshi_contracts_for_adoption,
  kalshi_position_leg,
  kalshi_sellable_contracts,
  order_still_resting,
  refresh_live_leg_contracts_from_kalshi,
  reconcile_close_stale_live_leg,
  resting_exit_order_id,
  resting_sell_contracts,
  sync_live_positions_from_kalshi,
  try_live_position_exit,
  verify_kalshi_exit_fill,
)


def _mock_kalshi_exit_order(kalshi: MagicMock, *, executed: bool = True) -> None:
  status = "executed" if executed else "resting"
  kalshi.get_order.return_value = {"status": status, "fill_count": 2}


@pytest.fixture(autouse=True)
def _no_exit_inventory_poll_sleep(monkeypatch):
  monkeypatch.setattr("src.trading.live_position_sync.time.sleep", lambda _s: None)


def test_kalshi_sellable_contracts_yes_and_no():
  kalshi = MagicMock()
  kalshi.authenticated = True
  kalshi.get_market_position.return_value = 4
  assert kalshi_sellable_contracts(kalshi, "T1", "yes") == 4
  assert kalshi_sellable_contracts(kalshi, "T1", "no") == 0

  kalshi.get_market_position.return_value = -3
  assert kalshi_sellable_contracts(kalshi, "T2", "no") == 3


def test_resting_exit_order_id_reads_latest_resting_row():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.open_position({
      "id": "p1",
      "event_ticker": "EV1",
      "market_ticker": "T1",
      "side": "yes",
      "contracts": 2,
      "entry_price_cents": 20,
      "cost_usd": 0.4,
      "mode": "live",
    })
    store.log_trade({
      "event_ticker": "EV1",
      "action": "exit",
      "status": "resting",
      "position_id": "p1",
      "kalshi_order_id": "ord-9",
    })
    assert resting_exit_order_id(store, "p1") == "ord-9"


def test_order_still_resting_matches_open_orders():
  kalshi = MagicMock()
  kalshi.list_resting_orders.return_value = [{"order_id": "ord-9"}]
  assert order_still_resting(kalshi, "ord-9") is True
  assert order_still_resting(kalshi, "ord-x") is False


def test_cancel_orphan_live_sell_orders():
  kalshi = MagicMock()
  kalshi.authenticated = True
  kalshi.list_resting_orders.return_value = [
    {"order_id": "a", "action": "sell", "ticker": "T-orphan"},
    {"order_id": "b", "action": "sell", "ticker": "T-open"},
    {"order_id": "c", "action": "buy", "ticker": "T-other"},
  ]
  n = cancel_orphan_live_sell_orders(kalshi, {"T-open"})
  assert n == 1
  kalshi.cancel_order.assert_called_once_with("a")


def test_hourly_event_market_tickers_from_tab_collects_contracts():
  tab = {
    "live": {
      "primary_pick": {"ticker": "KXTEST-1H-T1"},
      "strategy_threshold": {
        "best_edge": {"ticker": "KXTEST-1H-T2"},
        "contracts": [{"ticker": "KXTEST-1H-T3"}],
      },
      "strategy_range": {"most_likely": {"ticker": "KXTEST-1H-T4"}},
    }
  }
  tickers = hourly_event_market_tickers_from_tab(tab)
  assert tickers == {"KXTEST-1H-T1", "KXTEST-1H-T2", "KXTEST-1H-T3", "KXTEST-1H-T4"}


def test_cancel_resting_enter_orders_for_hourly_event_scoped():
  kalshi = MagicMock()
  kalshi.authenticated = True
  kalshi.list_resting_orders.return_value = [
    {"order_id": "e1", "action": "buy", "ticker": "KXTEST-1H-T1"},
    {"order_id": "e2", "action": "buy", "ticker": "WORLDCUP-T9"},
    {"order_id": "e3", "action": "sell", "ticker": "KXTEST-1H-T1"},
  ]
  tab = {"live": {"primary_pick": {"ticker": "KXTEST-1H-T1"}}}
  n = cancel_resting_enter_orders_for_hourly_event(kalshi, "KXTEST-1H", tab)
  assert n == 1
  kalshi.cancel_order.assert_called_once_with("e1")


def test_adopt_filled_resting_enter_opens_bot_leg():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.log_trade({
      "event_ticker": "EV1",
      "action": "enter",
      "status": "resting",
      "mode": "live",
      "market_ticker": "KXBTCD-EV1-T59400",
      "side": "yes",
      "contracts": 2,
      "price_cents": 40,
      "entry_price_cents": 40,
      "kalshi_order_id": "ord-rest",
      "label": "$59,400 or above",
    })
    kalshi = MagicMock()
    kalshi.authenticated = True
    kalshi.get_market_position.return_value = 2
    out = adopt_filled_resting_enters(store, kalshi, "EV1")
    open_pos = store.open_positions("KXBTCD-EV1")
    assert len(open_pos) == 1
    assert open_pos[0]["contracts"] == 2
    assert open_pos[0]["entry_price_cents"] == 40
    assert any(c.get("action") == "adopted_resting_enter" for c in out["changes"])


def test_adopt_filled_resting_enter_caps_contracts():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.log_trade({
      "event_ticker": "EV1",
      "action": "enter",
      "status": "resting",
      "mode": "live",
      "market_ticker": "KXBTCD-EV1-T59400",
      "side": "no",
      "contracts": 6,
      "price_cents": 50,
      "entry_price_cents": 50,
      "kalshi_order_id": "ord-big",
    })
    kalshi = MagicMock()
    kalshi.authenticated = True
    kalshi.get_market_position.return_value = -6
    cfg = {"hourly": {"bot": {"live_exit": {"max_adopted_contracts": 6}}}}
    adopt_filled_resting_enters(store, kalshi, "EV1", cfg=cfg, kind="hourly")
    open_pos = store.open_positions("KXBTCD-EV1")
    assert len(open_pos) == 1
    assert open_pos[0]["contracts"] == 6
    assert open_pos[0]["entry_source"] == "adopted_resting"


def test_adopt_filled_resting_enter_caps_at_six_when_kalshi_has_eight():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.log_trade({
      "event_ticker": "EV1",
      "action": "enter",
      "status": "resting",
      "mode": "live",
      "market_ticker": "KXBTCD-EV1-T59400",
      "side": "no",
      "contracts": 8,
      "price_cents": 50,
      "entry_price_cents": 50,
      "kalshi_order_id": "ord-big",
    })
    kalshi = MagicMock()
    kalshi.authenticated = True
    kalshi.get_market_position.return_value = -8
    cfg = {"hourly": {"bot": {"live_exit": {"max_adopted_contracts": 6}}}}
    adopt_filled_resting_enters(store, kalshi, "EV1", cfg=cfg, kind="hourly")
    open_pos = store.open_positions("KXBTCD-EV1")
    assert open_pos[0]["contracts"] == 6


def test_adopt_filled_resting_enter_cross_hour_event():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.log_trade({
      "event_ticker": "KXBTCD-26JUL0206",
      "action": "enter",
      "status": "resting",
      "mode": "live",
      "market_ticker": "KXBTCD-26JUL0206-T61600",
      "side": "yes",
      "contracts": 2,
      "price_cents": 40,
      "entry_price_cents": 40,
      "kalshi_order_id": "ord-rest",
      "label": "$61,600 or above",
    })
    kalshi = MagicMock()
    kalshi.authenticated = True
    kalshi.get_market_position.return_value = 2
    out = adopt_filled_resting_enters(store, kalshi, "KXBTCD-26JUL0210")
    open_pos = store.open_positions("KXBTCD-26JUL0206")
    assert len(open_pos) == 1
    assert any(c.get("action") == "adopted_resting_enter" for c in out["changes"])
    assert store.latest_resting_enter("KXBTCD-26JUL0206", "KXBTCD-26JUL0206-T61600") is None


def test_adopt_kalshi_orphan_inventory_opens_untracked_leg():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.log_trade({
      "event_ticker": "KXBTCD-26JUN3006",
      "action": "enter",
      "status": "filled",
      "mode": "live",
      "market_ticker": "KXBTCD-26JUN3006-T59399.99",
      "side": "no",
      "contracts": 2,
      "price_cents": 82,
      "entry_price_cents": 82,
      "label": "$59,400 or above",
      "signal": "BUY NO",
    })
    kalshi = MagicMock()
    kalshi.authenticated = True
    kalshi.list_market_positions.return_value = [{
      "ticker": "KXBTCD-26JUN3006-T59399.99",
      "position_fp": -2.0,
      "market_exposure_dollars": 1.64,
    }]
    kalshi.get_market_position.return_value = -2.0
    out = adopt_kalshi_orphan_inventory(store, kalshi, "KXBTCD-26JUN3006")
    open_pos = store.open_positions("KXBTCD-26JUN3006")
    assert len(open_pos) == 1
    assert open_pos[0]["contracts_fp"] == 2.0
    assert open_pos[0]["entry_price_cents"] == 82
    assert open_pos[0]["label"] == "$59,400 or above"
    assert any(c.get("action") == "adopted_kalshi_orphan" for c in out["changes"])


def test_try_live_position_exit_reconciles_when_kalshi_flat():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.open_position({
      "id": "p1",
      "event_ticker": "EV1",
      "market_ticker": "T1",
      "side": "yes",
      "contracts": 4,
      "entry_price_cents": 20,
      "cost_usd": 0.8,
      "mode": "live",
    })
    kalshi = MagicMock()
    kalshi.authenticated = True
    kalshi.get_market_position.return_value = 0
    kalshi.list_resting_orders.return_value = []
    row = try_live_position_exit(
      kalshi=kalshi,
      store=store,
      pos=store.open_positions("EV1")[0],
      period_key="EV1",
      exit_price=25,
      contracts=4,
      entry_c=20,
      pos_mode="live",
      pick={"signal": "BUY YES"},
      exit_reason="CUT LOSS",
      detail_suffix="test",
      extra_detail="",
    )
    assert row is not None
    assert row["status"] == "reconciled"
    assert "reconciled" in row["detail"].lower()
    assert store.open_positions("EV1") == []


def test_sync_live_positions_reconciles_when_kalshi_inventory_zero():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.open_position({
      "id": "p1",
      "event_ticker": "EV1",
      "market_ticker": "T1",
      "side": "yes",
      "contracts": 4,
      "entry_price_cents": 1,
      "cost_usd": 0.04,
      "mode": "live",
    })
    kalshi = MagicMock()
    kalshi.authenticated = True
    kalshi.get_market_position.return_value = 0
    kalshi.list_resting_orders.return_value = []
    out = sync_live_positions_from_kalshi(store, kalshi, "EV1")
    assert store.open_positions("EV1") == []
    assert any(c.get("action") == "reconciled_closed" for c in out["changes"])


def test_sync_reconciles_phantom_legs_from_other_hours():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.open_position({
      "id": "old-leg",
      "event_ticker": "KXBTCD-26JUL0209",
      "market_ticker": "KXBTCD-26JUL0209-T60000",
      "side": "yes",
      "contracts": 2,
      "entry_price_cents": 44,
      "cost_usd": 0.88,
      "mode": "live",
    })
    kalshi = MagicMock()
    kalshi.authenticated = True
    kalshi.get_market_position.return_value = 0
    kalshi.list_resting_orders.return_value = []
    out = sync_live_positions_from_kalshi(store, kalshi, "KXBTCD-26JUL0216")
    assert store.all_open_live_positions() == []
    assert any(c.get("action") == "reconciled_closed" for c in out["changes"])


def test_sync_does_not_reconcile_when_resting_exit_sell_open():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.open_position({
      "id": "p1",
      "event_ticker": "EV1",
      "market_ticker": "T1",
      "side": "yes",
      "contracts": 4,
      "entry_price_cents": 24,
      "cost_usd": 0.96,
      "mode": "live",
    })
    kalshi = MagicMock()
    kalshi.authenticated = True
    kalshi.get_market_position.return_value = 0
    kalshi.list_resting_orders.return_value = [
      {
        "order_id": "sell-1",
        "action": "sell",
        "ticker": "T1",
        "side": "yes",
        "remaining_count": 4,
      }
    ]
    out = sync_live_positions_from_kalshi(store, kalshi, "EV1")
    assert len(store.open_positions("EV1")) == 1
    assert not any(c.get("action") == "reconciled_closed" for c in out["changes"])


def test_try_live_position_exit_waits_when_resting_sell_open():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.open_position({
      "id": "p1",
      "event_ticker": "EV1",
      "market_ticker": "T1",
      "side": "yes",
      "contracts": 4,
      "entry_price_cents": 24,
      "cost_usd": 0.96,
      "mode": "live",
    })
    kalshi = MagicMock()
    kalshi.authenticated = True
    kalshi.get_market_position.return_value = 0
    kalshi.list_resting_orders.return_value = [
      {
        "order_id": "sell-1",
        "action": "sell",
        "ticker": "T1",
        "side": "yes",
        "remaining_count": 4,
      }
    ]
    out = try_live_position_exit(
      kalshi=kalshi,
      store=store,
      pos=store.open_positions("EV1")[0],
      period_key="EV1",
      exit_price=31,
      contracts=4,
      entry_c=24,
      pos_mode="live",
      pick={"signal": "BUY YES"},
      exit_reason="PROFIT TARGET",
      detail_suffix="test",
      extra_detail="",
    )
    assert out is None
    assert store.open_positions("EV1") != []


def test_resting_sell_contracts_counts_remaining_sells():
  kalshi = MagicMock()
  kalshi.authenticated = True
  kalshi.list_resting_orders.return_value = [
    {"action": "sell", "ticker": "T1", "side": "yes", "remaining_count": 3},
    {"action": "buy", "ticker": "T1", "side": "yes", "remaining_count": 4},
    {"action": "sell", "ticker": "T2", "side": "yes", "remaining_count": 1},
  ]
  assert resting_sell_contracts(kalshi, "T1", "yes") == 3
  assert effective_kalshi_inventory(kalshi, "T1", "yes") == 3


def test_reconcile_close_stale_live_leg_logs_and_closes():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.open_position({
      "id": "p1",
      "event_ticker": "EV1",
      "market_ticker": "T1",
      "side": "no",
      "contracts": 2,
      "entry_price_cents": 30,
      "cost_usd": 0.6,
      "mode": "live",
      "label": "test leg",
    })
    store.log_trade({
      "event_ticker": "EV1",
      "action": "exit",
      "status": "resting",
      "position_id": "p1",
      "contracts": 2,
      "entry_price_cents": 30,
      "exit_price_cents": 18,
      "price_cents": 18,
    })
    row = reconcile_close_stale_live_leg(
      store=store,
      pos=store.open_positions("EV1")[0],
      period_key="EV1",
    )
    assert row["status"] == "reconciled"
    assert row["pnl_usd"] == -0.24
    assert "loss" in row["detail"].lower()
    assert store.open_positions("EV1") == []


def test_kalshi_position_leg_fractional_no_with_exposure():
  kalshi = MagicMock()
  kalshi.authenticated = True
  kalshi.list_market_positions.return_value = [{
    "ticker": "T-B59450",
    "position_fp": -2.2,
    "market_exposure_dollars": 1.63,
  }]
  snap = kalshi_position_leg(kalshi, "T-B59450", "no")
  assert snap is not None
  assert snap["contracts"] == 2.2
  assert snap["cost_usd"] == 1.63
  assert snap["entry_price_cents"] == 74


def test_sync_live_positions_fractional_contracts_and_entry():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.open_position({
      "id": "p1",
      "event_ticker": "EV1",
      "market_ticker": "T-B59450",
      "side": "no",
      "contracts": 2,
      "entry_price_cents": 73,
      "cost_usd": 1.46,
      "mode": "live",
    })
    kalshi = MagicMock()
    kalshi.authenticated = True
    kalshi.list_market_positions.return_value = [{
      "ticker": "T-B59450",
      "position_fp": -2.2,
      "market_exposure_dollars": 1.63,
    }]
    kalshi.get_market_position.return_value = -2.2
    out = sync_live_positions_from_kalshi(store, kalshi, "EV1")
    open_pos = store.open_positions("EV1")
    assert len(open_pos) == 1
    assert open_pos[0]["contracts_fp"] == 2.2
    assert open_pos[0]["contracts"] == 2
    assert open_pos[0]["entry_price_cents"] == 74
    assert open_pos[0]["cost_usd"] == 1.63
    assert any(c.get("action") == "synced" for c in out["changes"])


def test_sync_live_positions_from_kalshi_updates_contracts_and_merges_duplicates():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.open_position({
      "id": "p1",
      "event_ticker": "EV1",
      "market_ticker": "T1",
      "side": "no",
      "contracts": 1,
      "entry_price_cents": 80,
      "cost_usd": 0.8,
      "mode": "live",
      "opened_at": "2026-01-01T00:00:00+00:00",
    })
    store.open_position({
      "id": "p2",
      "event_ticker": "EV1",
      "market_ticker": "T1",
      "side": "no",
      "contracts": 1,
      "entry_price_cents": 80,
      "cost_usd": 0.8,
      "mode": "live",
      "opened_at": "2026-01-01T00:01:00+00:00",
    })
    kalshi = MagicMock()
    kalshi.authenticated = True
    kalshi.list_market_positions.return_value = [{
      "ticker": "T1",
      "position_fp": -5,
      "market_exposure_dollars": 4.0,
    }]
    kalshi.get_market_position.return_value = -5
    out = sync_live_positions_from_kalshi(store, kalshi, "EV1")
    open_pos = store.open_positions("EV1")
    assert len(open_pos) == 1
    assert open_pos[0]["contracts"] == 5
    assert open_pos[0]["cost_usd"] == 4.0
    assert any(c.get("action") == "synced" for c in out["changes"])


def test_kalshi_contracts_for_adoption_uses_sellable_over_snap():
  snap = {"contracts": 2.0, "entry_price_cents": 70}
  cfg = {"hourly": {"bot": {"live_exit": {"max_adopted_contracts": 6}}}}
  contracts, contracts_fp, raw = kalshi_contracts_for_adoption(6.0, snap, cfg, kind="hourly")
  assert raw == 6.0
  assert contracts == 6
  assert contracts_fp == 6.0


def test_refresh_live_leg_contracts_from_kalshi_before_exit_pnl():
  """Adopted legs sync to full Kalshi size for profit checks at exit."""
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    pos = {
      "id": "p1",
      "event_ticker": "EV1",
      "market_ticker": "T1",
      "side": "yes",
      "contracts": 2,
      "entry_price_cents": 68,
      "cost_usd": 1.36,
      "mode": "live",
      "entry_source": "adopted_resting",
    }
    store.open_position(pos)
    kalshi = MagicMock()
    kalshi.authenticated = True
    kalshi.get_market_position.return_value = 3
    kalshi.list_market_positions.return_value = []
    refreshed = refresh_live_leg_contracts_from_kalshi(pos, kalshi, store)
    assert refreshed["contracts"] == 3
    assert refreshed["cost_usd"] == 2.04
    open_pos = store.open_positions("EV1")
    assert open_pos[0]["contracts"] == 3
    assert open_pos[0]["cost_usd"] == 2.04


def test_verify_kalshi_exit_fill_requires_inventory_drop():
  assert verify_kalshi_exit_fill(sellable_before=5.0, sellable_after=3.0, claimed_fill=2) == 2
  assert verify_kalshi_exit_fill(sellable_before=2.0, sellable_after=2.0, claimed_fill=2) == 0
  assert verify_kalshi_exit_fill(sellable_before=None, sellable_after=0.0, claimed_fill=1) == 0


def test_confirm_kalshi_exit_fill_requires_executed_when_api_claims_fill():
  assert confirm_kalshi_exit_fill(
    sellable_before=2.0,
    sellable_after=0.0,
    claimed_fill=2,
    order_status="executed",
  ) == 2
  assert confirm_kalshi_exit_fill(
    sellable_before=2.0,
    sellable_after=0.0,
    claimed_fill=2,
    order_status="resting",
  ) == 0
  assert confirm_kalshi_exit_fill(
    sellable_before=2.0,
    sellable_after=0.0,
    claimed_fill=2,
    order_status=None,
  ) == 2


def test_try_live_position_exit_rejects_unverified_api_fill_after_floor_retry():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.open_position({
      "id": "p1",
      "event_ticker": "EV1",
      "market_ticker": "T1",
      "side": "no",
      "contracts": 2,
      "entry_price_cents": 36,
      "cost_usd": 0.72,
      "mode": "live",
    })
    kalshi = MagicMock()
    kalshi.authenticated = True
    kalshi.get_market_position.return_value = -2
    kalshi.list_resting_orders.return_value = []
    _mock_kalshi_exit_order(kalshi, executed=False)
    kalshi.get_market_ticker.return_value = {"no_bid_dollars": "0.1600"}
    kalshi.create_order.side_effect = [
      {"order": {"order_id": "sell-1", "fill_count": 2, "remaining_count": 0, "status": "executed"}},
      {"order": {"order_id": "sell-2", "fill_count": 2, "remaining_count": 0, "status": "executed"}},
      {"order": {"order_id": "sell-3", "fill_count": 2, "remaining_count": 0, "status": "executed"}},
    ]
    row = try_live_position_exit(
      kalshi=kalshi,
      store=store,
      pos=store.open_positions("EV1")[0],
      period_key="EV1",
      exit_price=44,
      contracts=2,
      entry_c=36,
      pos_mode="live",
      pick={"signal": "BUY NO"},
      exit_reason="PROFIT TARGET",
      detail_suffix="test",
      extra_detail="",
    )
    assert row is not None
    assert row["status"] == "skipped"
    assert store.open_positions("EV1") != []
    assert kalshi.create_order.call_count == 3
    assert kalshi.cancel_order.called


def test_try_live_position_exit_retries_unverified_fill_with_floor_sell():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.open_position({
      "id": "p1",
      "event_ticker": "EV1",
      "market_ticker": "T1",
      "side": "no",
      "contracts": 2,
      "entry_price_cents": 36,
      "cost_usd": 0.72,
      "mode": "live",
    })
    kalshi = MagicMock()
    kalshi.authenticated = True
    kalshi.get_market_position.side_effect = [-2] + [-2] * 8 + [0]
    kalshi.list_resting_orders.return_value = []
    _mock_kalshi_exit_order(kalshi, executed=True)
    kalshi.create_order.side_effect = [
      {"order": {"order_id": "sell-1", "fill_count": 2, "remaining_count": 0, "status": "executed"}},
      {"order": {"order_id": "sell-2", "fill_count": 2, "remaining_count": 0, "status": "executed"}},
    ]
    out = try_live_position_exit(
      kalshi=kalshi,
      store=store,
      pos=store.open_positions("EV1")[0],
      period_key="EV1",
      exit_price=44,
      contracts=2,
      entry_c=36,
      pos_mode="live",
      pick={"signal": "BUY NO"},
      exit_reason="PROFIT TARGET",
      detail_suffix="test",
      extra_detail="",
    )
    assert out is not None
    assert out["fill_count"] == 2
    assert kalshi.create_order.call_count == 2
    retry_kwargs = kalshi.create_order.call_args_list[1].kwargs
    assert retry_kwargs["no_price"] == 1
    assert retry_kwargs["time_in_force"] == "fill_or_kill"


def test_try_live_position_exit_verifies_fill_after_stale_inventory_cache():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.open_position({
      "id": "p1",
      "event_ticker": "EV1",
      "market_ticker": "T1",
      "side": "no",
      "contracts": 2,
      "entry_price_cents": 36,
      "cost_usd": 0.72,
      "mode": "live",
    })
    kalshi = MagicMock()
    kalshi.authenticated = True
    kalshi.get_market_position.side_effect = [-2, -2, 0]
    kalshi.list_resting_orders.return_value = []
    _mock_kalshi_exit_order(kalshi, executed=True)
    kalshi.create_order.return_value = {
      "order": {"order_id": "sell-1", "fill_count": 2, "remaining_count": 0, "status": "executed"},
    }
    out = try_live_position_exit(
      kalshi=kalshi,
      store=store,
      pos=store.open_positions("EV1")[0],
      period_key="EV1",
      exit_price=44,
      contracts=2,
      entry_c=36,
      pos_mode="live",
      pick={"signal": "BUY NO"},
      exit_reason="PROFIT TARGET",
      detail_suffix="test",
      extra_detail="",
    )
    assert out is not None
    assert out["fill_count"] == 2
    assert kalshi.create_order.call_count == 1


def test_try_live_position_exit_cancels_stale_resting_exit_when_inventory_held():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.open_position({
      "id": "p1",
      "event_ticker": "EV1",
      "market_ticker": "T1",
      "side": "yes",
      "contracts": 4,
      "entry_price_cents": 20,
      "cost_usd": 0.8,
      "mode": "live",
    })
    store.log_trade({
      "event_ticker": "EV1",
      "action": "exit",
      "status": "resting",
      "position_id": "p1",
      "kalshi_order_id": "ord-1",
    })
    kalshi = MagicMock()
    kalshi.authenticated = True
    kalshi.get_market_position.side_effect = [4, 0]
    kalshi.list_resting_orders.return_value = [{"order_id": "ord-1"}]
    _mock_kalshi_exit_order(kalshi, executed=True)
    kalshi.create_order.return_value = {
      "order": {"order_id": "sell-2", "fill_count": 4, "remaining_count": 0, "status": "executed"},
    }
    out = try_live_position_exit(
      kalshi=kalshi,
      store=store,
      pos=store.open_positions("EV1")[0],
      period_key="EV1",
      exit_price=25,
      contracts=4,
      entry_c=20,
      pos_mode="live",
      pick={"signal": "BUY YES"},
      exit_reason="PROFIT TARGET",
      detail_suffix="test",
      extra_detail="",
    )
    assert out is not None
    assert out["fill_count"] == 4
    kalshi.cancel_order.assert_called_with("ord-1")
    assert kalshi.create_order.called


def test_try_live_position_exit_skips_when_pending_resting_exit():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.open_position({
      "id": "p1",
      "event_ticker": "EV1",
      "market_ticker": "T1",
      "side": "yes",
      "contracts": 4,
      "entry_price_cents": 20,
      "cost_usd": 0.8,
      "mode": "live",
    })
    store.log_trade({
      "event_ticker": "EV1",
      "action": "exit",
      "status": "resting",
      "position_id": "p1",
      "kalshi_order_id": "ord-1",
    })
    kalshi = MagicMock()
    kalshi.authenticated = True
    kalshi.get_market_position.return_value = 0
    kalshi.list_resting_orders.return_value = [
      {
        "order_id": "ord-1",
        "action": "sell",
        "ticker": "T1",
        "side": "yes",
        "remaining_count": 4,
      }
    ]
    out = try_live_position_exit(
      kalshi=kalshi,
      store=store,
      pos=store.open_positions("EV1")[0],
      period_key="EV1",
      exit_price=25,
      contracts=4,
      entry_c=20,
      pos_mode="live",
      pick={"signal": "BUY YES"},
      exit_reason="CUT LOSS",
      detail_suffix="test",
      extra_detail="",
    )
    assert out is None
    kalshi.create_order.assert_not_called()


def test_try_live_position_exit_bid_retry_after_two_ghost_fills():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.open_position({
      "id": "p1",
      "event_ticker": "EV1",
      "market_ticker": "T1",
      "side": "yes",
      "contracts": 2,
      "entry_price_cents": 60,
      "cost_usd": 1.2,
      "mode": "live",
    })
    kalshi = MagicMock()
    kalshi.authenticated = True
    def _inventory_after_bid_poll(*_a, **_k):
      if kalshi.create_order.call_count >= 3:
        return 0
      return 2

    kalshi.get_market_position.side_effect = _inventory_after_bid_poll
    kalshi.list_resting_orders.return_value = []
    kalshi.get_market_ticker.return_value = {"yes_bid_dollars": "0.8400"}

    def _order_status(*_a, **_k):
      if kalshi.create_order.call_count >= 3:
        return {"status": "executed", "fill_count": 2}
      return {"status": "resting", "fill_count": 2}

    kalshi.get_order.side_effect = _order_status
    kalshi.create_order.side_effect = [
      {"order": {"order_id": "sell-1", "fill_count": 2, "remaining_count": 0, "status": "executed"}},
      {"order": {"order_id": "sell-2", "fill_count": 2, "remaining_count": 0, "status": "executed"}},
      {"order": {"order_id": "sell-3", "fill_count": 2, "remaining_count": 0, "status": "executed"}},
    ]
    out = try_live_position_exit(
      kalshi=kalshi,
      store=store,
      pos=store.open_positions("EV1")[0],
      period_key="EV1",
      exit_price=84,
      contracts=2,
      entry_c=60,
      pos_mode="live",
      pick={"signal": "BUY YES"},
      exit_reason="PROFIT TARGET",
      detail_suffix="test",
      extra_detail="",
    )
    assert out is not None
    assert out["fill_count"] == 2
    assert out["sell_cents"] == 84
    assert kalshi.create_order.call_count == 3


def test_try_live_position_exit_restores_closed_leg_after_unverified():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    store.open_position({
      "id": "p1",
      "event_ticker": "EV1",
      "market_ticker": "T1",
      "side": "yes",
      "contracts": 2,
      "entry_price_cents": 60,
      "cost_usd": 1.2,
      "mode": "live",
    })
    pos = store.open_positions("EV1")[0]
    store.close_position("p1")
    kalshi = MagicMock()
    kalshi.authenticated = True
    kalshi.get_market_position.return_value = 2
    kalshi.list_resting_orders.return_value = []
    kalshi.get_market_ticker.return_value = {"yes_bid_dollars": "0.8400"}
    kalshi.get_order.return_value = {"status": "resting", "fill_count": 2}
    kalshi.create_order.side_effect = [
      {"order": {"order_id": "sell-1", "fill_count": 2, "remaining_count": 0, "status": "executed"}},
      {"order": {"order_id": "sell-2", "fill_count": 2, "remaining_count": 0, "status": "executed"}},
      {"order": {"order_id": "sell-3", "fill_count": 2, "remaining_count": 0, "status": "executed"}},
    ]
    row = try_live_position_exit(
      kalshi=kalshi,
      store=store,
      pos=pos,
      period_key="EV1",
      exit_price=84,
      contracts=2,
      entry_c=60,
      pos_mode="live",
      pick={"signal": "BUY YES"},
      exit_reason="PROFIT TARGET",
      detail_suffix="test",
      extra_detail="",
    )
    assert row is not None
    assert row["status"] == "skipped"
    open_pos = store.open_positions("EV1")
    assert len(open_pos) == 1
    assert open_pos[0]["side"] == "yes"
    assert open_pos[0]["contracts"] == 2
