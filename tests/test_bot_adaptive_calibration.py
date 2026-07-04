"""Tests for closed-loop bucket adaptive calibration."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.trading.bot_adaptive_calibration import (
  adaptive_calibration_cfg,
  adaptive_entry_allowed,
  compute_throttle_level,
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


def test_short_window_losses_throttles_not_pauses():
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
  assert state["buckets"][key]["state"] == "restricted"
  assert state["buckets"][key]["throttle_level"] == 2
  adapt = adaptive_entry_allowed(
    state, entry_price_cents=15, entry_spread_cents=None, cfg=cfg, kind="hourly", now=now,
  )
  assert adapt.ok
  assert adapt.edge_boost_cents == 8.0
  assert adapt.stake_mult == 0.5
  assert adapt.hint and adapt.hint.startswith("adaptive_throttle:2:")


def test_stats_recovery_releases_throttle_on_refresh():
  now = datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc)
  key = price_bucket_key(15)
  cfg = {"bot_adaptive_calibration": {}}
  paused_legacy = {
    "buckets": {
      key: {
        "state": "paused",
        "paused_until": (now + timedelta(hours=6)).isoformat(),
        "short_stats": {"trades": 4, "wins": 3, "win_rate": 0.75, "total_pnl_usd": 1.5},
        "long_stats": {"trades": 4, "wins": 3, "win_rate": 0.75, "total_pnl_usd": 1.5},
      }
    }
  }
  updated = refresh_adaptive_buckets([], paused_legacy, cfg, kind="hourly", now=now)
  assert updated["buckets"][key]["throttle_level"] == 0
  assert updated["buckets"][key]["state"] == "normal"
  assert "paused_until" not in updated["buckets"][key]


def test_marginal_stats_use_level_one_throttle():
  now = datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc)
  trades: list[dict] = []
  for i in range(4):
    ts = (now - timedelta(hours=1, minutes=i * 10)).isoformat()
    pnl = -0.3 if i < 3 else 0.1
    trades.extend(_exit_trade(pid=f"m{i}", entry_cents=35, pnl=pnl, exit_at=ts))
  cfg = {
    "bot_adaptive_calibration": {
      "short_min_trades": 4,
      "short_max_win_rate": 0.25,
      "short_min_loss_usd": 2.0,
    }
  }
  state = refresh_adaptive_buckets(trades, {}, cfg, kind="hourly", now=now)
  key = price_bucket_key(35)
  assert state["buckets"][key]["throttle_level"] == 1
  adapt = adaptive_entry_allowed(
    state, entry_price_cents=35, entry_spread_cents=None, cfg=cfg, kind="hourly",
  )
  assert adapt.edge_boost_cents == 4.0
  assert adapt.stake_mult == 0.75


def test_probe_hooks_are_noops():
  cfg = {"bot_adaptive_calibration": {}}
  key = price_bucket_key(12)
  state = {"buckets": {key: {"state": "probing", "probe_entries_remaining": 1}}}
  after_enter = record_adaptive_probe_entry(
    state, entry_price_cents=12, entry_spread_cents=None, cfg=cfg, kind="hourly",
  )
  assert after_enter["buckets"][key]["probe_entries_remaining"] == 1
  after_exit = record_adaptive_probe_exit(
    state, entry_price_cents=12, entry_spread_cents=None, pnl_usd=-0.25, cfg=cfg, kind="hourly",
  )
  assert after_exit["buckets"][key]["state"] == "probing"


def test_compute_throttle_level_long_window_severe():
  cfg = adaptive_calibration_cfg({"bot_adaptive_calibration": {}}, kind="hourly")
  short = {"trades": 4, "win_rate": 0.2, "total_pnl_usd": -2.5}
  long = {"trades": 10, "win_rate": 0.2, "total_pnl_usd": -6.0}
  assert compute_throttle_level(short, long, cfg) == 3


def test_slot15_disabled_by_default_despite_global_enabled():
  cfg = {
    "bot_adaptive_calibration": {"enabled": True},
    "intra_slot": {"bot": {}},
  }
  acfg = adaptive_calibration_cfg(cfg, kind="slot15")
  assert acfg["enabled"] is False
  adapt = adaptive_entry_allowed(
    {"buckets": {"price:1–20¢": {"state": "paused", "throttle_level": 3}}},
    entry_price_cents=10,
    entry_spread_cents=None,
    cfg=cfg,
    kind="slot15",
  )
  assert adapt.ok and adapt.edge_boost_cents == 0.0


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
