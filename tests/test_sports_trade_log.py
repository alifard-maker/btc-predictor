"""Tests for sports trade log display (live 24h + paper append)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.trading.sports_arb_store import SportsArbStore


def _insert_trade(
  store: SportsArbStore,
  *,
  mode: str,
  status: str,
  created_at: str,
  event: str = "EVT",
) -> None:
  store.log_trade(
    {
      "strategy": "value_sharp",
      "kind": "poly_value",
      "event_ticker": event,
      "edge_usd": 0.05,
      "total_cost_usd": 1.0,
      "selection": "Team A",
    },
    mode=mode,
    status=status,
    extra={},
  )
  with store._connect() as conn:
    conn.execute(
      "UPDATE sports_trades SET created_at = ? WHERE id = (SELECT MAX(id) FROM sports_trades)",
      (created_at,),
    )


def test_list_trades_for_display_pins_live_24h(tmp_path: Path):
  store = SportsArbStore(tmp_path / "sports.db")
  now = datetime.now(timezone.utc)
  old_live = (now - timedelta(hours=30)).isoformat()
  recent_live = (now - timedelta(hours=2)).isoformat()
  recent_paper = (now - timedelta(minutes=5)).isoformat()

  _insert_trade(store, mode="live", status="live_filled", created_at=old_live, event="OLD-LIVE")
  _insert_trade(store, mode="live", status="live_filled", created_at=recent_live, event="NEW-LIVE")
  for i in range(55):
    _insert_trade(
      store,
      mode="paper",
      status="paper_signal",
      created_at=recent_paper,
      event=f"PAPER-{i}",
    )

  plain = store.list_trades(limit=50)
  assert all(
    str(t.get("mode") or "").lower() != "live" or str(t.get("event_ticker")) != "OLD-LIVE"
    for t in plain
  )

  display = store.list_trades_for_display(live_retention_hours=24.0, paper_limit=40)
  live_events = {t["event_ticker"] for t in display["live_trades"]}
  assert "NEW-LIVE" in live_events
  assert "OLD-LIVE" not in live_events
  assert display["live_count"] == 1
  assert display["paper_count"] == 40
  assert any(t["event_ticker"] == "NEW-LIVE" for t in display["recent_trades"])


def test_fresh_start_preserves_live_trades(tmp_path: Path):
  store = SportsArbStore(tmp_path / "sports.db")
  now = datetime.now(timezone.utc).isoformat()
  _insert_trade(store, mode="live", status="live_filled", created_at=now, event="LIVE-1")
  _insert_trade(store, mode="paper", status="paper_signal", created_at=now, event="PAPER-1")

  result = store.fresh_start(preserve_live=True)
  assert result["live_trades_preserved"] is True
  assert result["paper_trades_cleared"] == 1

  trades = store.list_trades(limit=10)
  assert len(trades) == 1
  assert trades[0]["mode"] == "live"
  assert trades[0]["event_ticker"] == "LIVE-1"
