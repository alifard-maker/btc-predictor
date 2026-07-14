"""Tests for 24h probe entry churn caps and trial regime sync."""

from __future__ import annotations

from dataclasses import dataclass

from src.trading.probe_24h import (
  apply_probe_entry_estrat_overlay,
  probe_entry_cap_applies,
  probe_entry_churn_block_reason,
  probe_max_filled_enters_per_hour,
  trial_regime_sync_pause_when_live_blocked,
)


@dataclass
class _FakeSummaryStore:
  filled: int = 0
  resting: int = 0

  def hour_interval_summary(self, event_ticker: str, *, mode: str | None = None) -> dict:
    return {
      "filled_enter_count_this_hour": self.filled,
      "enter_count": self.filled,
      "resting_enter_count": self.resting,
    }


def _probe_cfg(**overrides) -> dict:
  block = {
    "enabled": True,
    "started_at": "2026-07-12T10:51:00+00:00",
    "stats_epoch_at": "2026-07-12T10:51:00+00:00",
    "max_filled_enters_per_hour": 2,
  }
  block.update(overrides)
  return {"pnl_first": {"probe_24h": block}}


def test_probe_entry_cap_applies_live_and_trial():
  cfg = _probe_cfg()
  assert probe_entry_cap_applies(cfg, kind="hourly", mode="live")
  assert probe_entry_cap_applies(cfg, kind="hourly_live", mode="live")
  assert probe_entry_cap_applies(cfg, kind="hourly_trial_mech", mode="paper")
  assert probe_entry_cap_applies(cfg, kind="hourly_trial", mode="paper")
  assert not probe_entry_cap_applies(cfg, kind="hourly", mode="paper")
  assert not probe_entry_cap_applies({"pnl_first": {}}, kind="hourly", mode="live")


def test_probe_entry_churn_blocks_at_cap():
  cfg = _probe_cfg()
  store = _FakeSummaryStore(filled=2, resting=0)
  reason = probe_entry_churn_block_reason(
    store, "KXBTCD-26JUL1212", cfg, kind="hourly", mode="live",
  )
  assert reason == "probe_24h_entry_cap:2>=2"


def test_probe_entry_churn_counts_resting_slots():
  cfg = _probe_cfg()
  store = _FakeSummaryStore(filled=1, resting=1)
  reason = probe_entry_churn_block_reason(
    store, "KXBTCD-26JUL1212", cfg, kind="hourly_trial_mech", mode="paper",
  )
  assert reason == "probe_24h_entry_cap:2>=2"


def test_probe_entry_churn_allows_under_cap():
  cfg = _probe_cfg()
  store = _FakeSummaryStore(filled=1, resting=0)
  assert probe_entry_churn_block_reason(
    store, "KXBTCD-26JUL1212", cfg, kind="hourly", mode="live",
  ) is None


def test_eth_trial_regime_sync_when_live_blocked():
  cfg = _probe_cfg()
  tab = {"live": {"regime": {"allow_trade": False, "reasons": ["Low vol"]}}}
  reason = trial_regime_sync_pause_when_live_blocked(
    tab, cfg, kind="hourly_trial", asset="eth",
  )
  assert reason and reason.startswith("trial_regime_sync:")


def test_btc_trial_mech_regime_sync_prefix():
  cfg = _probe_cfg()
  tab = {"regime": {"blocked": True, "reasons": ["chop"]}}
  reason = trial_regime_sync_pause_when_live_blocked(
    tab, cfg, kind="hourly_trial_mech", asset="btc",
  )
  assert reason and reason.startswith("trial_mech_regime_sync:")


def test_apply_probe_entry_estrat_overlay_disables_scale_in():
  from src.trading.entry_strategy import EntryStrategyConfig

  estrat = EntryStrategyConfig(
    allow_scale_in=True,
    max_entries_per_cycle=4,
    max_concurrent_positions=6,
  )
  out = apply_probe_entry_estrat_overlay(
    estrat, _probe_cfg(), kind="hourly", mode="live",
  )
  assert out.allow_scale_in is False
  assert out.max_entries_per_cycle == 1
  assert out.max_concurrent_positions == 2


def test_apply_probe_entry_estrat_overlay_allows_two_per_cycle_at_cap_four():
  from src.trading.entry_strategy import EntryStrategyConfig

  estrat = EntryStrategyConfig(
    allow_scale_in=True,
    max_entries_per_cycle=4,
    max_concurrent_positions=6,
  )
  out = apply_probe_entry_estrat_overlay(
    estrat, _probe_cfg(max_filled_enters_per_hour=4), kind="hourly", mode="live",
  )
  assert out.allow_scale_in is False
  assert out.max_entries_per_cycle == 2
  assert out.max_concurrent_positions == 4


def test_probe_max_filled_enters_defaults_to_two():
  assert probe_max_filled_enters_per_hour(_probe_cfg()) == 2
  assert probe_max_filled_enters_per_hour({"pnl_first": {}}) is None
