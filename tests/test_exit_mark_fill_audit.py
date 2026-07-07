"""Tests for live exit mark-vs-fill audit."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from src.trading.exit_mark_fill_audit import (
  build_exit_mark_fill_audit_report,
  enrich_exit_mark_fill_fields,
)


def test_enrich_exit_mark_fill_fields():
  ctx = enrich_exit_mark_fill_fields(
    {"exit_reason": "PROFIT TARGET"},
    peaks={"peak_unrealized_usd": 0.80, "peak_profit_pct": 0.4},
    decision_mark_cents=55,
    unrealized_at_decision_usd=0.45,
    fill_exit_cents=52,
    min_hold_seconds=90,
  )
  assert ctx["decision_mark_cents"] == 55
  assert ctx["fill_exit_cents"] == 52
  assert ctx["mark_vs_fill_cents"] == -3
  assert ctx["peak_unrealized_usd"] == 0.8
  assert ctx["min_hold_seconds"] == 90


def test_build_exit_mark_fill_audit_report():
  since = datetime(2026, 7, 4, tzinfo=timezone.utc)
  trades = [
    {
      "action": "exit",
      "status": "filled",
      "mode": "live",
      "created_at": "2026-07-05T12:00:00+00:00",
      "event_ticker": "KXBTCD-TEST",
      "market_ticker": "KXBTC-TEST",
      "contracts": 2,
      "entry_price_cents": 40,
      "exit_price_cents": 50,
      "pnl_usd": 0.20,
      "exit_context_json": json.dumps({
        "exit_reason": "PROFIT TARGET",
        "decision_mark_cents": 52,
        "fill_exit_cents": 50,
        "mark_vs_fill_cents": -2,
        "peak_unrealized_usd": 0.55,
        "unrealized_at_decision_usd": 0.24,
        "hold_seconds": 95,
        "min_hold_seconds": 90,
      }),
    },
    {
      "action": "exit",
      "status": "filled",
      "mode": "paper",
      "created_at": "2026-07-05T12:05:00+00:00",
      "contracts": 1,
      "entry_price_cents": 30,
      "exit_price_cents": 25,
      "pnl_usd": -0.05,
    },
  ]
  rep = build_exit_mark_fill_audit_report(trades, since=since)
  assert rep["closed_live_exits"] == 1
  assert rep["enriched_rows"] == 1
  assert rep["totals"]["total_realized_usd"] == 0.20
  assert rep["by_exit_reason"]["PROFIT TARGET"]["trades"] == 1
  assert rep["profit_exits_near_min_hold"]["count"] == 1
