"""Tests for live hourly vs paper trial hour-by-hour comparison."""

from __future__ import annotations

from pathlib import Path

from src.trading.hourly_bot_store import HourlyBotStore
from src.trading.hourly_live_trial_compare import build_hourly_live_trial_compare


def _log_trade(store: HourlyBotStore, **kwargs):
  base = {
    "event_ticker": "KXBTCD-26JUL0206",
    "trigger": "continuous",
    "action": "enter",
    "mode": "live",
    "market_ticker": "KXBTCD-26JUL0206-T95000",
    "side": "yes",
    "contracts": 10,
    "price_cents": 42,
    "entry_price_cents": 42,
    "cost_usd": 4.20,
    "status": "filled",
    "label": "≥ $95,000",
  }
  base.update(kwargs)
  store.log_trade(base)


def test_compare_matched_hour_entries_exits_and_pnl(tmp_path: Path):
  live_db = tmp_path / "hourly_bot_btc.db"
  trial_db = tmp_path / "hourly_trial_bot_btc.db"
  live_store = HourlyBotStore(live_db)
  trial_store = HourlyBotStore(trial_db)

  _log_trade(
    live_store,
    created_at="2026-07-02T10:05:00+00:00",
    mode="live",
  )
  _log_trade(
    live_store,
    action="exit",
    exit_price_cents=55,
    pnl_usd=1.30,
    exit_context={"exit_reason": "TAKE PROFIT"},
    created_at="2026-07-02T10:40:00+00:00",
    mode="live",
  )

  _log_trade(
    trial_store,
    created_at="2026-07-02T10:06:00+00:00",
    mode="paper",
  )
  _log_trade(
    trial_store,
    action="exit",
    exit_price_cents=48,
    pnl_usd=0.60,
    exit_context={"exit_reason": "CUT LOSSES"},
    created_at="2026-07-02T10:35:00+00:00",
    mode="paper",
  )

  out = build_hourly_live_trial_compare(
    live_store,
    trial_store,
    asset="btc",
    limit_hours=5,
    live_mode="live",
    trial_mode="paper",
  )

  assert out["ok"] is True
  assert out["matched_event_count"] == 1
  assert len(out["hours"]) >= 1
  hour = out["hours"][0]
  assert hour["event_ticker"] == "KXBTCD-26JUL0206"
  assert hour["both_active"] is True
  assert len(hour["live"]["entries"]) == 1
  assert len(hour["trial"]["entries"]) == 1
  assert hour["live"]["exits"][0]["exit_reason"] == "TAKE PROFIT"
  assert hour["trial"]["exits"][0]["exit_reason"] == "CUT LOSSES"
  assert hour["live"]["net_pnl_usd"] == 1.30
  assert hour["trial"]["net_pnl_usd"] == 0.60
  assert hour["pnl_delta_usd"] == 0.70


def test_compare_filters_by_mode(tmp_path: Path):
  live_db = tmp_path / "hourly_bot_btc.db"
  trial_db = tmp_path / "hourly_trial_bot_btc.db"
  live_store = HourlyBotStore(live_db)
  trial_store = HourlyBotStore(trial_db)

  _log_trade(live_store, mode="paper", created_at="2026-07-02T11:00:00+00:00")
  _log_trade(trial_store, mode="paper", created_at="2026-07-02T11:01:00+00:00")

  out = build_hourly_live_trial_compare(
    live_store,
    trial_store,
    asset="btc",
    limit_hours=5,
    live_mode="live",
    trial_mode="paper",
  )

  live_hour = next((h for h in out["hours"] if h["event_ticker"] == "KXBTCD-26JUL0206"), None)
  assert live_hour is not None
  assert live_hour["live"]["has_activity"] is False
  assert live_hour["trial"]["has_activity"] is True
