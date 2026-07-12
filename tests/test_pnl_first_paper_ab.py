"""Tests for ETH paper A/B report and timing filters."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.trading.eth_paper_experiment import eth_experiment_start_at, seed_eth_paper_settings_from_cfg
from src.trading.hourly_bot_store import HourlyBotSettings, HourlyBotStore
from src.trading.pnl_first_paper_ab import build_paper_ab_report, write_paper_ab_report
from src.trading.trade_timing_analytics import build_trade_timing_report


@pytest.fixture
def eth_cfg():
  return {
    "pnl_first": {"mid_hour_entry": {"eth_paper_enabled": True}},
    "eth": {
      "hourly": {
        "bot": {
          "enabled": True,
          "mode": "paper",
          "continuous_enabled": True,
          "experiment_start_at": "2026-07-09T12:00:00+00:00",
          "paper_experiment": {"enabled": True, "sync_settings_on_arm": True},
        },
      },
    },
  }


def _sample_trades():
  return [
    {
      "id": "e1",
      "action": "enter",
      "status": "filled",
      "mode": None,
      "position_id": "p1",
      "event_ticker": "KXETHD-26JUL0912",
      "created_at": "2026-07-09T11:50:00+00:00",
      "entry_settings": {"hours_to_settle": 20 / 60},
      "market_ticker": "KXETHD-26JUL0912-T3500",
      "side": "yes",
    },
    {
      "action": "exit",
      "status": "filled",
      "mode": None,
      "position_id": "p1",
      "event_ticker": "KXETHD-26JUL0912",
      "created_at": "2026-07-09T12:20:00+00:00",
      "market_ticker": "KXETHD-26JUL0912-T3500",
      "side": "yes",
      "contracts": 2,
      "entry_price_cents": 40,
      "exit_price_cents": 45,
      "pnl_usd": 0.10,
      "exit_context": {"hours_to_settle": 5 / 60, "exit_reason": "TAKE PROFIT"},
    },
    {
      "id": "e2",
      "action": "enter",
      "status": "filled",
      "mode": "paper",
      "position_id": "p2",
      "event_ticker": "KXETHD-26JUL0913",
      "created_at": "2026-07-09T12:30:00+00:00",
      "entry_settings": {"hours_to_settle": 30 / 60},
      "market_ticker": "KXETHD-26JUL0913-T3500",
      "side": "yes",
    },
    {
      "action": "exit",
      "status": "reconciled",
      "mode": "paper",
      "position_id": "p2",
      "event_ticker": "KXETHD-26JUL0913",
      "created_at": "2026-07-09T12:45:00+00:00",
      "market_ticker": "KXETHD-26JUL0913-T3500",
      "side": "yes",
      "contracts": 1,
      "entry_price_cents": 50,
      "exit_price_cents": 48,
      "pnl_usd": -0.02,
      "exit_context": {"hours_to_settle": 15 / 60, "exit_reason": "LEG STOP"},
    },
    {
      "action": "exit",
      "status": "filled",
      "mode": "live",
      "position_id": "p3",
      "event_ticker": "KXETHD-26JUL0913",
      "created_at": "2026-07-09T12:50:00+00:00",
      "pnl_usd": -1.00,
    },
  ]


def test_eth_experiment_start_at_reads_yaml(eth_cfg):
  start = eth_experiment_start_at(eth_cfg)
  assert start == datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)


def test_build_trade_timing_report_counts_null_mode_as_paper():
  since = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)
  rep = build_trade_timing_report(
    _sample_trades(),
    mode="paper",
    since=since,
    since_field="exit",
  )
  assert rep["closed_legs"] == 2
  assert rep["total_pnl_usd"] == 0.08


def test_seed_eth_paper_settings_sets_epoch_when_settings_unchanged(tmp_path: Path, eth_cfg):
  store = HourlyBotStore(tmp_path / "eth.db")
  store.save_settings(HourlyBotSettings.from_dict({
    "enabled": True,
    "mode": "paper",
    "continuous": True,
    "paper_auto_refill": True,
    "profit_use_pct": 100.0,
    "max_spend_per_hour_usd": 15.0,
  }))

  result = seed_eth_paper_settings_from_cfg(store, eth_cfg)
  assert result["ok"] is True

  with store._connect() as conn:
    from src.trading.bot_runtime import stats_epoch_at

    assert stats_epoch_at(conn) == "2026-07-09T12:00:00+00:00"


def test_build_paper_ab_report_uses_eth_epoch(monkeypatch, eth_cfg, tmp_path):
  store = HourlyBotStore(tmp_path / "eth.db")
  store.save_settings(HourlyBotSettings.from_dict({
    "enabled": True,
    "mode": "paper",
    "continuous": True,
    "max_spend_per_hour_usd": 15.0,
  }))
  for trade in _sample_trades():
    store.log_trade(trade)

  loop = MagicMock()
  loop.hourly_bot_store.return_value = store

  monkeypatch.setattr(
    "src.trading.pnl_first_paper_ab.build_kalshi_live_report",
    lambda *_a, **_k: {
      "ok": True,
      "closed_legs": 1,
      "total_pnl_usd": -0.50,
      "by_exit_type": [],
      "by_entry_timing": [],
    },
  )
  monkeypatch.setattr(
    "src.trading.pnl_first_paper_ab.experiment_epoch_at",
    lambda *_a, **_k: datetime(2026, 7, 4, 16, 59, tzinfo=timezone.utc),
  )

  report = build_paper_ab_report(loop, eth_cfg)
  assert report["eth_epoch_start_at"] == "2026-07-09T12:00:00+00:00"
  assert report["eth_paper"]["closed_legs"] == 2
  assert report["eth_paper"]["total_pnl_usd"] == 0.08
  assert report["eth_live"]["enabled"] is False


def test_write_paper_ab_report_persists(monkeypatch, eth_cfg, tmp_path):
  monkeypatch.setenv("DATA_DIR", str(tmp_path))
  store = HourlyBotStore(tmp_path / "eth.db")
  store.save_settings(HourlyBotSettings.from_dict({
    "enabled": True,
    "mode": "paper",
    "continuous": True,
    "max_spend_per_hour_usd": 15.0,
  }))
  for trade in _sample_trades():
    store.log_trade(trade)

  loop = MagicMock()
  loop.hourly_bot_store.return_value = store
  monkeypatch.setattr(
    "src.trading.pnl_first_paper_ab.build_kalshi_live_report",
    lambda *_a, **_k: {"ok": True, "closed_legs": 0, "total_pnl_usd": 0.0},
  )
  monkeypatch.setattr(
    "src.trading.pnl_first_paper_ab.experiment_epoch_at",
    lambda *_a, **_k: datetime(2026, 7, 4, 16, 59, tzinfo=timezone.utc),
  )

  payload = write_paper_ab_report(loop, eth_cfg)
  out = tmp_path / "logs" / "pnl_first_manager" / "paper_ab_latest.json"
  assert out.exists()
  assert payload["eth_paper"]["closed_legs"] == 2
