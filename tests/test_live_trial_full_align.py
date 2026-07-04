"""Full live↔trial alignment: trial_legs, leg-stop cooldown, mirror stake/scale-in."""

from __future__ import annotations

from unittest.mock import MagicMock

from src.trading.bot_cheap_leg_cooldown import (
  is_in_leg_stop_event_cooldown,
  is_label_cut_cooldown_reason,
  leg_stop_reentry_cooldown_seconds,
  migrate_leg_stop_event_cooldowns,
  record_leg_stop_event_cooldown,
  resolve_label_reentry_cooldown_seconds,
)
from src.trading.hourly_bot_store import HourlyBotStore
from src.trading.hourly_live_trial_align import (
  apply_mirror_trial_entry_estrat,
  leg_stop_entry_blocked,
  mirror_trial_live_contract_count,
  should_use_trial_leg_exits,
)
from src.trading.entry_strategy import EntryStrategyConfig


def _cfg() -> dict:
  return {
    "hourly": {
      "bot": {
        "live_trial_align": {
          "enabled": True,
          "live_exit_mode": "trial_legs",
          "exits": {
            "leg_stop_reentry_cooldown_seconds": 600,
            "leg_stop_event_cooldown_seconds": 300,
          },
          "stake": {
            "max_stake_per_entry_usd": 4.0,
            "max_budget_fraction_per_entry": 0.30,
            "max_contracts_per_entry": 8,
          },
          "execution": {
            "mirror_trial_stake_sizing": True,
            "mirror_trial_scale_in": True,
          },
        },
        "entry_strategy": {
          "allow_scale_in": True,
          "scale_in_max_legs_per_ticker": 4,
          "max_stake_per_entry_usd": 3.5,
        },
      }
    }
  }


def test_trial_legs_all_hours(tmp_path):
  assert should_use_trial_leg_exits(
    _cfg(), kind="hourly", mode="live", hold_seconds=5000,
    adaptive_mode="rally", hour_momentum_state="normal",
  )


def test_leg_stop_label_cooldown():
  assert is_label_cut_cooldown_reason("LEG STOP")
  assert resolve_label_reentry_cooldown_seconds(
    "LEG STOP", _cfg(), bot_kind="hourly",
  ) == 600


def test_leg_stop_event_cooldown(tmp_path):
  import sqlite3

  db = tmp_path / "bot.db"
  store = HourlyBotStore(db)
  with store._connect() as conn:
    migrate_leg_stop_event_cooldowns(conn)
    record_leg_stop_event_cooldown(conn, "EV1", cooldown_seconds=300)
    assert is_in_leg_stop_event_cooldown(conn, "EV1")
  assert store.is_in_leg_stop_event_cooldown("EV1")
  assert leg_stop_entry_blocked(store, "EV1", cfg=_cfg(), mode="live") == "leg_stop_event_cooldown"


def test_mirror_stake_contract_count():
  estrat = EntryStrategyConfig(max_stake_per_entry_usd=1.0, max_contracts_per_entry=2)
  pick = {
    "yes_bid": 66,
    "yes_ask": 68,
    "kalshi_mid": 0.67,
  }
  count = mirror_trial_live_contract_count(
    pick=pick,
    side="yes",
    stake_usd=3.40,
    price_cents=68,
    max_spend_per_hour_usd=15.0,
    estrat=estrat,
    cfg=_cfg(),
    kind="hourly",
    mode="live",
  )
  assert 1 <= count <= 2  # mirror preview then cap_live_entry_contracts (stake + max_contracts)


def test_mirror_scale_in_estrat():
  base = EntryStrategyConfig(allow_scale_in=False, max_stake_per_entry_usd=2.0)
  out = apply_mirror_trial_entry_estrat(base, _cfg(), kind="hourly", mode="live")
  assert out.allow_scale_in is True
  assert out.max_stake_per_entry_usd == 4.0
