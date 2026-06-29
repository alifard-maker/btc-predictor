"""Tests for shared bot risk gates and daily loss cap."""

from __future__ import annotations

import tempfile
from pathlib import Path

from src.trading.bot_risk_gates import (
  SKIP_DAILY_CAP,
  apply_daily_loss_cap_to_stores,
  record_exit_and_maybe_cap,
  risk_gate_skip_reason,
  sync_auto_stop_for_risk,
)
from src.trading.bot_risk_state import BotRiskCoordinator, DailyLossConfig, register_bot_stores
from src.trading.hourly_bot_store import HourlyBotStore


def test_daily_loss_cap_stops_all_stores():
  with tempfile.TemporaryDirectory() as td:
    data_dir = Path(td)
    coord = BotRiskCoordinator(data_dir, DailyLossConfig(enabled=True, cap_usd=10.0))
    from src.trading import bot_risk_state as brs

    brs._COORDINATOR = coord
    store_a = HourlyBotStore(data_dir / "a.db")
    store_b = HourlyBotStore(data_dir / "b.db")
    register_bot_stores([store_a, store_b])

    hit = record_exit_and_maybe_cap(-12.0)
    assert hit is True
    assert coord.is_cap_active()
    apply_daily_loss_cap_to_stores([store_a, store_b])
    assert store_a.get_settings().auto_stopped is True
    assert store_a.get_settings().auto_stop_reason == SKIP_DAILY_CAP
    assert risk_gate_skip_reason() == SKIP_DAILY_CAP


def test_sync_auto_stop_clears_when_cap_lifted():
  with tempfile.TemporaryDirectory() as td:
    data_dir = Path(td)
    store = HourlyBotStore(data_dir / "bot.db")
    store.save_settings(
      type(store.get_settings())(
        **{**store.get_settings().to_dict(), "auto_stopped": True, "auto_stop_reason": SKIP_DAILY_CAP}
      )
    )
    coord = BotRiskCoordinator(data_dir, DailyLossConfig(enabled=True, cap_usd=50.0))
    from src.trading import bot_risk_state as brs

    brs._COORDINATOR = coord
    sync_auto_stop_for_risk(store)
    s = store.get_settings()
    assert s.auto_stopped is False
    assert s.auto_stop_reason is None


def test_kalshi_stale_auto_stop_cleared():
  with tempfile.TemporaryDirectory() as td:
    from src.trading import kalshi_circuit as kc

    data_dir = Path(td)
    store = HourlyBotStore(data_dir / "bot.db")
    store.save_settings(
      type(store.get_settings())(
        **{
          **store.get_settings().to_dict(),
          "auto_stopped": True,
          "auto_stop_reason": "kalshi_api_paused",
        }
      )
    )
    sync_auto_stop_for_risk(store)
    s = store.get_settings()
    assert s.auto_stopped is False
    assert s.auto_stop_reason is None


def test_kalshi_degraded_does_not_auto_stop():
  with tempfile.TemporaryDirectory() as td:
    from src.trading import kalshi_circuit as kc

    data_dir = Path(td)
    cb = kc.KalshiCircuitBreaker(kc.CircuitConfig(warn_threshold=2), data_dir / "c.json")
    kc._STATE = cb
    store = HourlyBotStore(data_dir / "bot.db")
    cb.record_failure("e1")
    cb.record_failure("e2")
    from src.trading.bot_risk_gates import SKIP_KALSHI_DEGRADED

    assert risk_gate_skip_reason() == SKIP_KALSHI_DEGRADED
    sync_auto_stop_for_risk(store)
    assert not store.get_settings().auto_stopped
