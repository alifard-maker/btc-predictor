"""Manual settle-timing pause removed — buys always allowed."""

from src.trading.hourly_regime import (
  entry_too_far_for_manual_skip_reason,
  max_hours_to_settle_for_manual_entry,
)


def test_manual_never_pauses_on_settle_timing():
  cfg = {"human_trading": {"max_hours_to_settle_for_entry": 1.35}}
  assert entry_too_far_for_manual_skip_reason(0.8, cfg) is None
  assert entry_too_far_for_manual_skip_reason(6.7, cfg) is None
  assert entry_too_far_for_manual_skip_reason(15.0, cfg) is None
  assert max_hours_to_settle_for_manual_entry(cfg) == 1.35
