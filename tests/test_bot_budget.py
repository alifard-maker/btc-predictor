"""Tests for deployable bankroll and config budget sync."""

from __future__ import annotations

import tempfile
from pathlib import Path

from src.trading.bot_budget import (
  config_max_spend_per_hour,
  remaining_budget_usd,
  sync_max_spend_from_config,
)
from src.trading.hourly_bot_store import HourlyBotSettings, HourlyBotStore


class _LiveBudgetSettings:
  mode = "live"
  use_accumulated_profit = False
  profit_use_pct = 100.0
  live_auto_refill_hour_budget = False


def test_config_max_spend_per_hour_reads_hourly_bot():
  cfg = {"hourly": {"bot": {"max_spend_per_hour_usd": 10}}}
  assert config_max_spend_per_hour(cfg) == 10.0


def test_sync_max_spend_clamps_stored_cap_down_to_config():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "hourly_bot_btc.db")
    store.save_settings(HourlyBotSettings(enabled=True, max_spend_per_hour_usd=25.0))
    sync_max_spend_from_config(store, cfg={"hourly": {"bot": {"max_spend_per_hour_usd": 10}}})
    assert store.get_settings().max_spend_per_hour_usd == 10.0


def test_sync_max_spend_does_not_raise_stored_cap():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "hourly_bot_btc.db")
    store.save_settings(HourlyBotSettings(enabled=True, max_spend_per_hour_usd=8.0))
    sync_max_spend_from_config(store, cfg={"hourly": {"bot": {"max_spend_per_hour_usd": 10}}})
    assert store.get_settings().max_spend_per_hour_usd == 8.0


def test_live_remaining_budget_uses_concurrent_cap_after_churn():
  """Cumulative enters can exceed max at-risk; deployable follows open exposure."""
  settings = _LiveBudgetSettings()
  remaining = remaining_budget_usd(
    settings=settings,
    max_cap=40.0,
    paper_bankroll_usd=0.0,
    interval_realized_pnl_usd=0.16,
    open_exposure_usd=0.86,
    interval_total_entered_usd=42.0,
  )
  assert remaining == 39.14


def test_live_auto_refill_uses_concurrent_cap_not_cumulative_entered():
  """Auto-refill ON still caps deployable by open exposure, not churn total."""
  settings = _LiveBudgetSettings()
  settings.live_auto_refill_hour_budget = True
  remaining = remaining_budget_usd(
    settings=settings,
    max_cap=30.0,
    paper_bankroll_usd=0.0,
    interval_realized_pnl_usd=-0.75,
    open_exposure_usd=6.56,
    interval_total_entered_usd=24.31,
  )
  assert remaining == 22.69


def test_live_remaining_budget_frees_after_exit():
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    max_cap = 40.0
    store.save_settings(HourlyBotSettings(
      enabled=True,
      max_spend_per_hour_usd=max_cap,
      mode="live",
      use_accumulated_profit=False,
    ))
    store.log_trade({
      "event_ticker": "EV1",
      "action": "enter",
      "mode": "live",
      "status": "filled",
      "cost_usd": 15.0,
    })
    store.log_trade({
      "event_ticker": "EV1",
      "action": "exit",
      "mode": "live",
      "status": "filled",
      "pnl_usd": 0.16,
    })
    store.log_trade({
      "event_ticker": "EV1",
      "action": "enter",
      "mode": "live",
      "status": "filled",
      "cost_usd": 12.0,
    })
    store.log_trade({
      "event_ticker": "EV1",
      "action": "exit",
      "mode": "live",
      "status": "filled",
      "pnl_usd": 0.0,
    })
    store.open_position({
      "id": "p1",
      "event_ticker": "EV1",
      "market_ticker": "T1",
      "side": "yes",
      "contracts": 2,
      "entry_price_cents": 43,
      "cost_usd": 0.86,
      "mode": "live",
    })
    assert store.open_exposure_usd("EV1", mode="live") == 0.86
    assert store.remaining_budget_usd("EV1", max_cap) == 39.14
