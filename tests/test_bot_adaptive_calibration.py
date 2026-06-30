"""Tests for closed-loop bucket adaptive calibration."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.trading.bot_adaptive_calibration import (
  adaptive_calibration_cfg,
  adaptive_entry_allowed,
  price_bucket_key,
  record_adaptive_probe_entry,
  record_adaptive_probe_exit,
  refresh_adaptive_buckets,
  run_adaptive_calibration_for_store,
)


def _exit_trade(
  *,
  pid: str,
  entry_cents: int,
  pnl: float,
  exit_at: str,
) -> list[dict]:
  return [
    {
      "action": "enter",
      "status": "filled",
      "position_id": pid,
      "entry_price_cents": entry_cents,
      "price_cents": entry_cents,
      "created_at": exit_at,
    },
    {
      "action": "exit",
      "status": "filled",
      "position_id": pid,
      "entry_price_cents": entry_cents,
      "pnl_usd": pnl,
      "created_at": exit_at,
    },
  ]


def _losing_short_window_trades(now: datetime) -> list[dict]:
  trades: list[dict] = []
  for i in range(4):
    ts = (now - timedelta(hours=1, minutes=i * 10)).isoformat()
    trades.extend(_exit_trade(pid=f"p{i}", entry_cents=15, pnl=-1.5, exit_at=ts))
  return trades


def test_short_window_losses_pause_bucket():
  now = datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc)
  cfg = {
    "bot_adaptive_calibration": {
      "short_min_trades": 4,
      "short_max_win_rate": 0.25,
      "short_min_loss_usd": 2.0,
    }
  }
  state = refresh_adaptive_buckets(
    _losing_short_window_trades(now), {}, cfg, kind="hourly", now=now,
  )
  key = price_bucket_key(15)
  assert state["buckets"][key]["state"] == "paused"
  ok, reason, _ = adaptive_entry_allowed(
    state, entry_price_cents=15, entry_spread_cents=None, cfg=cfg, kind="hourly", now=now,
  )
  assert not ok
  assert reason and reason.startswith("adaptive_bucket_paused:")


def test_pause_expiry_moves_to_probing():
  now = datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc)
  key = price_bucket_key(15)
  state = {
    "buckets": {
      key: {
        "state": "paused",
        "paused_until": (now - timedelta(minutes=1)).isoformat(),
      }
    }
  }
  updated = refresh_adaptive_buckets([], state, {"bot_adaptive_calibration": {}}, kind="hourly", now=now)
  assert updated["buckets"][key]["state"] == "probing"
  assert updated["buckets"][key]["probe_entries_remaining"] == 2


def test_probe_win_resumes_normal():
  cfg = {"bot_adaptive_calibration": {"probe_pause_hours_on_fail": 6}}
  key = price_bucket_key(12)
  state = {
    "buckets": {
      key: {"state": "probing", "probe_entries_remaining": 1},
    }
  }
  updated = record_adaptive_probe_exit(
    state,
    entry_price_cents=12,
    entry_spread_cents=None,
    pnl_usd=0.5,
    cfg=cfg,
    kind="hourly",
  )
  assert updated["buckets"][key]["state"] == "normal"


def test_probe_loss_repauses():
  cfg = {"bot_adaptive_calibration": {"probe_pause_hours_on_fail": 6}}
  key = price_bucket_key(12)
  state = {
    "buckets": {
      key: {"state": "probing", "probe_entries_remaining": 1},
    }
  }
  updated = record_adaptive_probe_exit(
    state,
    entry_price_cents=12,
    entry_spread_cents=None,
    pnl_usd=-0.25,
    cfg=cfg,
    kind="hourly",
  )
  assert updated["buckets"][key]["state"] == "paused"
  assert updated["buckets"][key]["paused_until"]


def test_tightened_bucket_adds_edge_boost():
  key = price_bucket_key(35)
  state = {"buckets": {key: {"state": "tightened"}}}
  ok, reason, boost = adaptive_entry_allowed(
    state,
    entry_price_cents=35,
    entry_spread_cents=None,
    cfg={"bot_adaptive_calibration": {"tightened_edge_boost_cents": 4}},
    kind="hourly",
  )
  assert ok and reason is None
  assert boost == 4.0


def test_probe_entry_decrements_allowance():
  key = price_bucket_key(10)
  state = {
    "buckets": {
      key: {"state": "probing", "probe_entries_remaining": 2},
    }
  }
  updated = record_adaptive_probe_entry(
    state,
    entry_price_cents=10,
    entry_spread_cents=None,
    cfg={"bot_adaptive_calibration": {}},
    kind="hourly",
  )
  assert updated["buckets"][key]["probe_entries_remaining"] == 1


def test_expired_pause_probe_entry_decrements_without_refresh():
  """Probe counter must update even when DB still says paused but pause expired."""
  now = datetime(2026, 6, 26, 18, 0, tzinfo=timezone.utc)
  key = price_bucket_key(10)
  state = {
    "buckets": {
      key: {
        "state": "paused",
        "paused_until": (now - timedelta(minutes=5)).isoformat(),
      },
    }
  }
  updated = record_adaptive_probe_entry(
    state,
    entry_price_cents=10,
    entry_spread_cents=None,
    cfg={"bot_adaptive_calibration": {"probe_max_entries": 2}},
    kind="hourly",
  )
  assert updated["buckets"][key]["state"] == "probing"
  assert updated["buckets"][key]["probe_entries_remaining"] == 1


def test_slot15_disabled_by_default_despite_global_enabled():
  cfg = {
    "bot_adaptive_calibration": {"enabled": True},
    "intra_slot": {"bot": {}},
  }
  acfg = adaptive_calibration_cfg(cfg, kind="slot15")
  assert acfg["enabled"] is False
  ok, _, _ = adaptive_entry_allowed(
    {"buckets": {"price:1–20¢": {"state": "paused", "paused_until": "2099-01-01T00:00:00+00:00"}}},
    entry_price_cents=10,
    entry_spread_cents=None,
    cfg=cfg,
    kind="slot15",
  )
  assert ok


def test_slot15_can_opt_in_via_intra_slot_bot():
  cfg = {
    "bot_adaptive_calibration": {"enabled": True},
    "intra_slot": {"bot": {"bot_adaptive_calibration": {"enabled": True}}},
  }
  acfg = adaptive_calibration_cfg(cfg, kind="slot15")
  assert acfg["enabled"] is True


class _FakeStore:
  def __init__(self, trades: list[dict]):
    self._trades = trades
    self._state: dict = {}

  def list_trades(self, *, limit: int = 5000, event_ticker: str | None = None):
    return self._trades[:limit]

  def get_adaptive_calibration(self):
    return self._state

  def save_adaptive_calibration(self, state):
    self._state = state
    return state


def test_run_for_store_skips_when_slot15_disabled():
  now = datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc)
  cfg = {"bot_adaptive_calibration": {"enabled": True}, "intra_slot": {"bot": {}}}
  store = _FakeStore(_losing_short_window_trades(now))
  out = run_adaptive_calibration_for_store(store, cfg=cfg, kind="slot15")
  assert out["ok"] is False
  assert out["reason"] == "adaptive_calibration_disabled"
