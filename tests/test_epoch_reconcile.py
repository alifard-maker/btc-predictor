"""Tests for epoch reconcile report."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from src.trading.epoch_reconcile import build_epoch_reconcile_report


def _cfg():
  return {
    "pnl_first": {"phase_started_at": "2026-07-04T23:30:00+00:00"},
    "hourly": {"bot": {"experiment_start_at": "2026-07-04T16:59:00+00:00"}},
  }


def test_build_epoch_reconcile_report_groups_bot_exits(tmp_path):
  from src.trading.hourly_bot_store import HourlyBotStore

  store = HourlyBotStore(tmp_path / "btc.db")
  epoch = datetime(2026, 7, 4, 23, 30, tzinfo=timezone.utc)
  store.log_trade({
    "event_ticker": "KXBTCD-26JUL0413",
    "trigger": "test",
    "action": "exit",
    "mode": "paper",
    "status": "filled",
    "pnl_usd": 1.25,
    "created_at": "2026-07-04T23:45:00+00:00",
  })
  store.log_trade({
    "event_ticker": "KXBTCD-26JUL0414",
    "trigger": "test",
    "action": "exit",
    "mode": "paper",
    "status": "filled",
    "pnl_usd": -0.50,
    "created_at": "2026-07-05T00:45:00+00:00",
  })
  store.log_trade({
    "event_ticker": "KXBTCD-26JUL0413",
    "trigger": "test",
    "action": "exit",
    "mode": "paper",
    "status": "filled",
    "pnl_usd": 0.75,
    "created_at": "2026-07-05T00:15:00+00:00",
  })
  store.log_trade({
    "event_ticker": "KXBTCD-26JUL0312",
    "trigger": "test",
    "action": "exit",
    "mode": "paper",
    "status": "filled",
    "pnl_usd": 9.99,
    "created_at": "2026-07-03T12:00:00+00:00",
  })

  loop = MagicMock()
  loop.hourly_bot_store.return_value = store
  loop.kalshi = None

  report = build_epoch_reconcile_report(loop, _cfg(), asset="btc")
  assert report["ok"] is True
  assert report["epoch_start_at"] == epoch.isoformat()
  assert report["totals"]["bot_pnl"] == 1.5
  rows = {r["event_ticker"]: r for r in report["rows"]}
  assert rows["KXBTCD-26JUL0413"]["bot_pnl"] == 2.0
  assert rows["KXBTCD-26JUL0413"]["bot_trades"] == 2
  assert rows["KXBTCD-26JUL0414"]["bot_pnl"] == -0.5


def test_build_epoch_reconcile_report_includes_kalshi_per_event(monkeypatch):
  from src.trading.hourly_bot_store import HourlyBotStore

  store = HourlyBotStore(tmp_path := __import__("tempfile").mkdtemp() + "/btc.db")
  store.log_trade({
    "event_ticker": "KXBTCD-26JUL0413",
    "trigger": "test",
    "action": "exit",
    "mode": "live",
    "status": "filled",
    "pnl_usd": 2.0,
    "created_at": "2026-07-05T01:00:00+00:00",
  })

  kalshi = MagicMock(authenticated=True)
  kalshi.list_fills.return_value = []

  def _summarize(_kalshi, *, since, asset=None, event_ticker=None, **kwargs):
    if event_ticker == "KXBTCD-26JUL0413":
      return {"ok": True, "total_pnl_usd": 1.75, "closed_trades": 2}
    return {"ok": True, "total_pnl_usd": 0.0, "closed_trades": 0}

  monkeypatch.setattr(
    "src.trading.kalshi_fill_sync.summarize_kalshi_experiment_fills",
    _summarize,
  )
  monkeypatch.setattr(
    "src.trading.epoch_reconcile._kalshi_events_since_epoch",
    lambda *_a, **_k: {"KXBTCD-26JUL0413"},
  )

  loop = MagicMock()
  loop.hourly_bot_store.return_value = store
  loop._kalshi_for.return_value = kalshi

  report = build_epoch_reconcile_report(loop, _cfg(), asset="btc")
  row = report["rows"][0]
  assert row["event_ticker"] == "KXBTCD-26JUL0413"
  assert row["bot_pnl"] == 2.0
  assert row["kalshi_pnl"] == 1.75
  assert row["drift"] == 0.25
  assert report["totals"]["drift"] == 0.25
