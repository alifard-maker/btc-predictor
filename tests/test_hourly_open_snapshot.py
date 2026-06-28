"""Tests for :00 ET hour-open hourly snapshots."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.db.hourly_store import SqliteHourlyStore
from src.models.hourly_snapshot import hour_open_prediction_from_row, locked_prediction_from_row
from src.trading.hourly_guidance import build_hourly_guidance


def _sample_row(**overrides):
  row = {
    "logged_at": "2026-06-28T16:00:00+00:00",
    "event_ticker": "KXBTCD-28JUN1600",
    "frequency": "hourly",
    "settle_time": "2026-06-28T17:00:00+00:00",
    "reference_price": 100000.0,
    "terminal_mu": 100050.0,
    "terminal_sigma": 120.0,
    "blended_mu": 100050.0,
    "primary_label": "Above $100,000",
    "primary_signal": "BUY YES",
    "primary_model_prob": 0.62,
    "primary_edge": 0.08,
    "regime_blocked": 0,
    "regime_notes": "",
    "settlement_zone_low": 99900.0,
    "settlement_zone_high": 100200.0,
    "method": "blend",
    "confidence": 0.24,
    "direction": "UP",
  }
  row.update(overrides)
  return row


def test_open_snapshot_store_roundtrip():
  with tempfile.TemporaryDirectory() as tmp:
    store = SqliteHourlyStore(str(Path(tmp) / "hourly.db"), asset="btc")
    store.init()
    row = _sample_row()
    rid = store.log_open_snapshot(row)
    assert rid > 0
    loaded = store.get_open_snapshot(row["event_ticker"])
    assert loaded is not None
    assert loaded["primary_signal"] == "BUY YES"
    assert loaded["reference_price"] == 100000.0

    row2 = {**row, "primary_signal": "NEUTRAL", "logged_at": "2026-06-28T16:05:00+00:00"}
    rid2 = store.log_open_snapshot(row2)
    assert rid2 == rid
    updated = store.get_open_snapshot(row["event_ticker"])
    assert updated["primary_signal"] == "NEUTRAL"

    assert store.log_prediction(row) > 0
    assert store.get_by_event_ticker(row["event_ticker"]) is not None
    assert store.get_open_snapshot(row["event_ticker"]) is not None


def test_hour_open_prediction_from_row_flags():
  row = _sample_row()
  out = hour_open_prediction_from_row(row, index_label="BRTI")
  assert out["hour_open"] is True
  assert out["locked"] is False
  assert out["snapshot_kind"] == "hour_open"
  assert "Hour-open BRTI" in out["most_likely"]["summary"]

  locked = locked_prediction_from_row(row)
  assert locked["locked"] is True
  assert locked.get("hour_open") is not True
  assert "Locked BRTI" in locked["most_likely"]["summary"]


def test_guidance_mentions_hour_open_before_lock():
  live = {
    "event": {"frequency": "hourly", "series_ticker": "KXBTCD"},
    "hours_to_settle": 0.9,
    "terminal_mu": 100100,
    "terminal_sigma": 100,
    "strategy_range": {"most_likely": {"label": "$100k band", "model_prob": 0.4}},
    "regime": {"allow_trade": True, "reasons": []},
  }
  hour_open = hour_open_prediction_from_row(_sample_row(), index_label="BRTI")
  out = build_hourly_guidance(live, locked=None, hour_open=hour_open, asset="btc", index_id="BRTI")
  assert ":00" in out["locked_vs_live"]["hour_open"]
  locked_rec = next(r for r in out["recommendations"] if r["tier"] == "locked")
  assert ":05" in locked_rec["reason"]


def test_schedule_hourly_registers_open_and_lock_jobs():
  from src.scheduler.loop import PredictionLoop

  loop = object.__new__(PredictionLoop)
  loop.cfg = {"timezone": "America/New_York", "hourly": {"enabled": True, "hour_open_snapshot": True}}
  loop.tz = "America/New_York"
  loop._eth_cfg = None
  loop.eth_hourly_calibration = None
  scheduler = MagicMock()
  with patch("src.scheduler.loop.asset_enabled", return_value=False):
    loop._schedule_hourly(scheduler)
  ids = [call.kwargs.get("id") for call in scheduler.add_job.call_args_list]
  assert "hourly_open" in ids
  assert "hourly_predict" in ids
  assert "hourly_late_call" in ids
