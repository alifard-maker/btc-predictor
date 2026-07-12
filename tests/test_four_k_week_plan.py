"""Tests for $4k/week plan lane P&L and report cache."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.trading.four_k_week_plan import (
  _lane_pnl_from_store,
  build_four_k_week_plan_report,
  build_four_k_week_plan_report_cached,
  invalidate_four_k_week_plan_cache,
)
from src.trading.hourly_bot_store import HourlyBotSettings, HourlyBotStore


def _seed_enter_exit(
  store: HourlyBotStore,
  *,
  mode: str,
  event: str,
  created_at: str,
  pnl: float = 1.25,
) -> None:
  store.log_trade({
    "id": f"{event}-enter",
    "event_ticker": event,
    "action": "enter",
    "mode": mode,
    "market_ticker": "MKT",
    "side": "yes",
    "contracts": 5,
    "price_cents": 40,
    "entry_price_cents": 40,
    "cost_usd": 2.0,
    "status": "filled",
    "position_id": event,
    "created_at": created_at,
  })
  store.log_trade({
    "id": f"{event}-exit",
    "event_ticker": event,
    "action": "exit",
    "mode": mode,
    "market_ticker": "MKT",
    "side": "yes",
    "contracts": 5,
    "price_cents": 65,
    "entry_price_cents": 40,
    "exit_price_cents": 65,
    "pnl_usd": pnl,
    "status": "filled",
    "position_id": event,
    "created_at": created_at,
  })


def test_lane_pnl_from_store_filters_epoch_and_mode(tmp_path: Path):
  store = HourlyBotStore(tmp_path / "lane.db")
  _seed_enter_exit(store, mode="live", event="H1", created_at="2026-07-12T16:00:00+00:00", pnl=2.0)
  _seed_enter_exit(store, mode="live", event="H0", created_at="2026-07-12T10:00:00+00:00", pnl=-1.0)
  _seed_enter_exit(store, mode="paper", event="H1P", created_at="2026-07-12T16:00:00+00:00", pnl=0.5)

  from datetime import datetime, timezone

  since = datetime.fromisoformat("2026-07-12T15:30:00+00:00")
  live = _lane_pnl_from_store(store, mode="live", since=since)
  assert live["net_pnl_usd"] == 2.0
  assert live["filled_enters"] == 1
  assert live["periods_with_entries"] == 1
  assert "H1" in live["_entry_events"]


@pytest.fixture
def plan_cfg(tmp_path, monkeypatch):
  monkeypatch.setenv("DATA_DIR", str(tmp_path))
  return {
    "pnl_first": {
      "four_k_week_plan": {
        "enabled": True,
        "week": 1,
        "started_at": "2026-07-12T15:30:00+00:00",
      },
      "probe_24h": {
        "enabled": True,
        "stats_epoch_at": "2026-07-12T15:30:00+00:00",
        "max_filled_enters_per_hour": 2,
        "min_ask_edge_cents": 18,
      },
      "track_b_shadow": {
        "enabled": True,
        "started_at": "2026-07-12T15:30:00+00:00",
        "stats_epoch_at": "2026-07-12T15:30:00+00:00",
        "assets": ["eth"],
      },
    },
    "eth": {
      "enabled": True,
      "intra_slot": {
        "bot": {
          "enabled": True,
          "mode": "paper",
          "experiment_start_at": "2026-07-12T15:30:00+00:00",
        },
      },
    },
  }


def _make_loop(tmp_path: Path, cfg: dict) -> SimpleNamespace:
  stores: dict[tuple[str, str], HourlyBotStore] = {}

  def hourly_bot_store(asset: str, *, kind: str = "hourly"):
    key = (asset, kind)
    if key not in stores:
      stores[key] = HourlyBotStore(tmp_path / f"{kind}_{asset}.db")
      mode = "live" if "live" in kind else "paper"
      stores[key].save_settings(
        HourlyBotSettings(enabled=True, mode=mode, continuous=True),
        source="test",
      )
    return stores[key]

  def slot15_bot_store(asset: str):
    key = ("slot15", asset)
    if key not in stores:
      from src.trading.slot15_bot_store import Slot15BotSettings, Slot15BotStore

      stores[key] = Slot15BotStore(tmp_path / f"slot15_{asset}.db")
      stores[key].save_settings(
        Slot15BotSettings(enabled=True, mode="paper", continuous=True),
        source="test",
      )
    return stores[key]

  return SimpleNamespace(hourly_bot_store=hourly_bot_store, slot15_bot_store=slot15_bot_store, cfg=cfg)


def test_build_four_k_week_plan_report_track_a_lightweight(tmp_path: Path, plan_cfg):
  loop = _make_loop(tmp_path, plan_cfg)
  live = loop.hourly_bot_store("eth", kind="hourly_live")
  trial = loop.hourly_bot_store("eth", kind="hourly_trial")
  _seed_enter_exit(live, mode="live", event="KXETH-H1", created_at="2026-07-12T16:00:00+00:00", pnl=3.0)
  _seed_enter_exit(trial, mode="paper", event="KXETH-H1", created_at="2026-07-12T16:00:00+00:00", pnl=1.0)

  report = build_four_k_week_plan_report(loop, plan_cfg)
  assert report["ok"] is True
  ta = report["lanes"]["track_a"]
  assert ta["live"]["net_pnl_usd"] == 3.0
  assert ta["trial"]["net_pnl_usd"] == 1.0
  assert ta["live"]["matched_hours"] == 1


def test_build_four_k_week_plan_report_cached(plan_cfg, monkeypatch):
  invalidate_four_k_week_plan_cache()
  calls = {"n": 0}
  sentinel = {"ok": True, "plan": "4k_week", "week": 1, "lanes": {}}

  def fake_build(loop, cfg):
    calls["n"] += 1
    return dict(sentinel)

  monkeypatch.setattr(
    "src.trading.four_k_week_plan.build_four_k_week_plan_report",
    fake_build,
  )
  loop = SimpleNamespace()

  first = build_four_k_week_plan_report_cached(loop, plan_cfg, ttl_sec=60.0)
  second = build_four_k_week_plan_report_cached(loop, plan_cfg, ttl_sec=60.0)

  assert calls["n"] == 1
  assert first.get("cached") is False
  assert second.get("cached") is True
  assert second.get("cache_age_sec") is not None


def test_cached_stale_on_db_busy(plan_cfg, monkeypatch):
  invalidate_four_k_week_plan_cache()
  loop = SimpleNamespace()
  good = {"ok": True, "plan": "4k_week", "week": 1, "lanes": {"track_a": {}}}
  state = {"n": 0}

  def flaky_build(_loop, _cfg):
    state["n"] += 1
    if state["n"] == 1:
      return dict(good)
    raise sqlite3.OperationalError("database is locked")

  monkeypatch.setattr(
    "src.trading.four_k_week_plan.build_four_k_week_plan_report",
    flaky_build,
  )

  fresh = build_four_k_week_plan_report_cached(loop, plan_cfg, ttl_sec=0.0)
  assert fresh["ok"] is True

  stale = build_four_k_week_plan_report_cached(loop, plan_cfg, ttl_sec=0.0)
  assert stale["stale"] is True
  assert stale["stale_reason"] == "db_busy"
  assert stale["week"] == 1
