"""Stats epoch should filter by event settle time, not trade created_at."""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

from src.trading.bot_runtime import event_in_stats_epoch, set_stats_epoch_at
from src.trading.hourly_bot_store import HourlyBotStore


def _exit_trade(event_ticker: str, pnl_usd: float, *, created_at: str) -> dict:
  return {
    "event_ticker": event_ticker,
    "action": "exit",
    "mode": "live",
    "status": "filled",
    "pnl_usd": pnl_usd,
    "created_at": created_at,
  }


def test_event_in_stats_epoch_uses_settle_not_late_exit_timestamp():
  epoch = "2026-07-04T16:59:00+00:00"
  # Jul 3 22:00 ET hour settles 2026-07-04 02:00 UTC — before epoch.
  assert not event_in_stats_epoch("KXBTCD-26JUL0322", epoch)
  # Jul 4 13:00 ET hour settles 2026-07-04 18:00 UTC — after epoch.
  assert event_in_stats_epoch("KXBTCD-26JUL0413", epoch)


def test_interval_stats_exclude_pre_epoch_hours_with_late_rollover_exits():
  epoch = "2026-07-04T16:59:00+00:00"
  with tempfile.TemporaryDirectory() as tmp:
    store = HourlyBotStore(Path(tmp) / "bot.db")
    with store._connect() as conn:
      set_stats_epoch_at(conn, epoch)

    # Late rollover exit logged after epoch on a pre-epoch hour.
    store.log_trade(
      _exit_trade(
        "KXBTCD-26JUL0322",
        0.0,
        created_at="2026-07-04T18:44:00+00:00",
      )
    )
    store.log_trade(
      _exit_trade(
        "KXBTCD-26JUL0413",
        1.25,
        created_at="2026-07-04T18:50:00+00:00",
      )
    )

    perf = store.interval_performance("KXBTCD-26JUL0415", mode="live")
    assert perf["profit_intervals"] == 1
    assert perf["loss_intervals"] == 0
    assert perf["intervals_scored"] == 1
    assert perf["net_interval_pnl_usd"] == 1.25


def test_synthetic_event_tickers_fall_back_to_first_trade_at():
  epoch = "2026-07-04T16:59:00+00:00"
  assert not event_in_stats_epoch(
    "HOUR-OLD",
    epoch,
    first_trade_at="2026-07-04T10:00:00+00:00",
  )
  assert event_in_stats_epoch(
    "HOUR-NEW",
    epoch,
    first_trade_at="2026-07-04T17:30:00+00:00",
  )
