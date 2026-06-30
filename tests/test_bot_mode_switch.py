"""Paper vs live position tagging and live-switch cleanup."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from src.trading.bot_period_rollover import close_paper_positions_for_period
from src.trading.bot_position_mode import exposure_by_mode, normalize_position_mode
from src.trading.hourly_bot_store import HourlyBotStore


def test_normalize_position_mode_defaults_unknown_to_paper():
  assert normalize_position_mode(None) == "paper"
  assert normalize_position_mode("live") == "live"
  assert normalize_position_mode("bogus") == "paper"


def test_exposure_by_mode_splits_costs():
  positions = [
    {"cost_usd": 6.0, "mode": "paper"},
    {"cost_usd": 4.0, "mode": "live"},
    {"cost_usd": 2.0},
  ]
  paper, live, total = exposure_by_mode(positions)
  assert paper == 8.0
  assert live == 4.0
  assert total == 12.0


def test_open_position_persists_mode(tmp_path: Path):
  db = tmp_path / "hourly.sqlite"
  store = HourlyBotStore(db)
  store.open_position({
    "event_ticker": "EV1",
    "market_ticker": "M1",
    "side": "no",
    "contracts": 3,
    "entry_price_cents": 80,
    "cost_usd": 2.4,
    "mode": "live",
  })
  pos = store.open_positions("EV1")[0]
  assert pos["mode"] == "live"
  split = store.open_exposure_by_mode_usd("EV1")
  assert split == {"paper": 0.0, "live": 2.4, "total": 2.4}


def test_remaining_budget_ignores_other_mode_exposure(tmp_path: Path):
  db = tmp_path / "hourly.sqlite"
  store = HourlyBotStore(db)
  settings = store.get_settings()
  settings = type(settings)(**{**settings.to_dict(), "mode": "live", "max_spend_per_hour_usd": 10.0})
  store.save_settings(settings)
  store.open_position({
    "event_ticker": "EV1",
    "market_ticker": "M1",
    "side": "no",
    "contracts": 10,
    "entry_price_cents": 80,
    "cost_usd": 8.0,
    "mode": "paper",
  })
  remaining = store.remaining_budget_usd("EV1", 10.0, settings)
  assert remaining == 10.0


def test_close_paper_positions_on_live_switch(tmp_path: Path):
  db = tmp_path / "hourly.sqlite"
  store = HourlyBotStore(db)
  opened = store.open_position({
    "event_ticker": "EV1",
    "market_ticker": "M1",
    "side": "yes",
    "contracts": 5,
    "entry_price_cents": 60,
    "cost_usd": 3.0,
    "mode": "paper",
    "last_mark_cents": 70,
  })
  closed = close_paper_positions_for_period(store, "EV1")
  assert len(closed) == 1
  assert store.open_positions("EV1") == []
  trades = store.list_trades(event_ticker="EV1")
  assert trades[0]["action"] == "exit"
  assert trades[0]["mode"] == "paper"
  assert trades[0]["trigger"] == "mode_switch"
  assert trades[0]["position_id"] == opened["id"]
