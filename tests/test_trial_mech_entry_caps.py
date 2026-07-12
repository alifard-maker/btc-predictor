"""Trial mech entry caps from yaml."""

from __future__ import annotations

from src.trading.entry_strategy import EntryStrategyConfig, entry_strategy_from_cfg


def test_trial_mech_entry_strategy_caps_from_yaml():
  cfg = {
    "hourly": {
      "bot": {
        "entry_strategy": {"max_contracts_per_entry": 6, "max_stake_per_entry_usd": 3.5},
        "trial_mech": {
          "entry_strategy": {
            "max_contracts_per_entry": 2,
            "max_stake_per_entry_usd": 2.50,
          }
        },
      }
    }
  }
  estrat = entry_strategy_from_cfg(cfg, kind="hourly_trial_mech")
  assert estrat.max_contracts_per_entry == 2
  assert estrat.max_stake_per_entry_usd == 2.50
