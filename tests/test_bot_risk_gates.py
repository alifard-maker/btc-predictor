"""Tests for shared bot risk gates and daily loss cap."""

from __future__ import annotations

import tempfile
from pathlib import Path

from src.trading.bot_risk_gates import (
  SKIP_DAILY_CAP,
  override_daily_loss_cap,
  record_exit_and_maybe_cap,
  risk_gate_skip_reason,
  sync_auto_stop_for_risk,
)
from src.trading.bot_risk_state import BotRiskCoordinator, DailyLossConfig, bot_risk_key
from src.trading.hourly_bot_store import HourlyBotStore


def test_daily_loss_cap_stops_only_that_bot():
  with tempfile.TemporaryDirectory() as td:
    data_dir = Path(td)
    coord = BotRiskCoordinator(data_dir, DailyLossConfig(enabled=True, cap_usd=10.0))
    from src.trading import bot_risk_state as brs

    brs._COORDINATOR = coord
    store_a = HourlyBotStore(data_dir / "a.db")
    store_b = HourlyBotStore(data_dir / "b.db")

    hit = record_exit_and_maybe_cap(-12.0, kind="hourly", asset="btc", store=store_a)
    assert hit is True
    assert coord.is_cap_active(bot_risk_key("hourly", "btc"))
    assert not coord.is_cap_active(bot_risk_key("hourly", "eth"))
    sync_auto_stop_for_risk(store_a, bot_key=bot_risk_key("hourly", "btc"))
    sync_auto_stop_for_risk(store_b, bot_key=bot_risk_key("hourly", "eth"))
    assert store_a.get_settings().auto_stopped is True
    assert store_a.get_settings().auto_stop_reason == SKIP_DAILY_CAP
    assert not store_b.get_settings().auto_stopped
    assert risk_gate_skip_reason(bot_key=bot_risk_key("hourly", "btc")) == SKIP_DAILY_CAP
    assert risk_gate_skip_reason(bot_key=bot_risk_key("hourly", "eth")) is None


def test_override_daily_cap_resumes_bot_for_today():
  with tempfile.TemporaryDirectory() as td:
    data_dir = Path(td)
    coord = BotRiskCoordinator(data_dir, DailyLossConfig(enabled=True, cap_usd=10.0))
    from src.trading import bot_risk_state as brs

    brs._COORDINATOR = coord
    store = HourlyBotStore(data_dir / "bot.db")
    key = bot_risk_key("hourly", "btc")
    record_exit_and_maybe_cap(-12.0, kind="hourly", asset="btc", store=store)
    assert coord.is_cap_active(key)
    st = override_daily_loss_cap(store, kind="hourly", asset="btc")
    assert st["cap_override"] is True
    assert not coord.is_cap_active(key)
    assert not store.get_settings().auto_stopped
    record_exit_and_maybe_cap(-20.0, kind="hourly", asset="btc", store=store)
    assert not coord.is_cap_active(key)


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
    sync_auto_stop_for_risk(store, bot_key=bot_risk_key("hourly", "btc"))
    s = store.get_settings()
    assert s.auto_stopped is False
    assert s.auto_stop_reason is None


def test_kalshi_stale_auto_stop_cleared():
  with tempfile.TemporaryDirectory() as td:
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
    sync_auto_stop_for_risk(store, bot_key=bot_risk_key("hourly", "btc"))
    s = store.get_settings()
    assert s.auto_stopped is False
    assert s.auto_stop_reason is None


def test_kalshi_degraded_does_not_block_entries():
  with tempfile.TemporaryDirectory() as td:
    from src.trading import kalshi_circuit as kc

    data_dir = Path(td)
    cb = kc.KalshiCircuitBreaker(kc.CircuitConfig(warn_threshold=2), data_dir / "c.json")
    kc._STATE = cb
    store = HourlyBotStore(data_dir / "bot.db")
    cb.record_failure("e1")
    cb.record_failure("e2")

    assert risk_gate_skip_reason(bot_key=bot_risk_key("hourly", "btc")) is None
    sync_auto_stop_for_risk(store, bot_key=bot_risk_key("hourly", "btc"))
    assert not store.get_settings().auto_stopped
