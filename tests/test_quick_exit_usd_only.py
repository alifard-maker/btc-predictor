"""Quick-exit USD-only profit trigger after min_hold."""

from __future__ import annotations

from src.trading.bot_live_exit import overlay_live_profit_settings
from src.trading.bot_profit_exit import should_take_profit_target
from src.trading.hourly_bot_store import HourlyBotSettings


def test_quick_exit_usd_only_fires_at_four_cents_after_hold():
  settings = HourlyBotSettings(min_hold_seconds=90, take_profit_pct=0.25)
  cfg = {
    "hourly": {
      "bot": {
        "quick_exit": {
          "enabled": True,
          "min_hold_seconds": 30,
          "take_profit_usd": 0.04,
        }
      }
    }
  }
  out = overlay_live_profit_settings(
    settings,
    {"entry_price_cents": 45},
    cfg,
    mode="live",
    adaptive_mode="defense",
  )
  assert out.take_profit_pct == 0.0
  assert out.take_profit_usd == 0.04
  assert should_take_profit_target(
    enabled=True,
    unrealized_usd=0.04,
    cost_usd=1.50,
    take_profit_pct=out.take_profit_pct,
    take_profit_usd=out.take_profit_usd,
    min_hold_seconds=out.min_hold_seconds,
    hold_seconds=30.0,
    profit_threshold_either=True,
  )
  assert not should_take_profit_target(
    enabled=True,
    unrealized_usd=0.04,
    cost_usd=1.50,
    take_profit_pct=out.take_profit_pct,
    take_profit_usd=out.take_profit_usd,
    min_hold_seconds=out.min_hold_seconds,
    hold_seconds=20.0,
    profit_threshold_either=True,
  )
