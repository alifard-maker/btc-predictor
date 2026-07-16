"""Manual settle window vs twin mid-hour bot gate."""

from src.trading.hourly_regime import (
  entry_too_far_for_manual_skip_reason,
  entry_too_far_from_settle_skip_reason,
  max_hours_to_settle_for_manual_entry,
)


def test_manual_allows_early_current_hour_bot_blocks():
  cfg = {
    "hourly": {"bot": {"max_hours_to_settle_for_entry": 0.75}},
    "human_trading": {"max_hours_to_settle_for_entry": 1.35},
  }
  # 0.8h left = ~12m into the hour — twin bot mid-hour blocks; manual must allow.
  assert entry_too_far_from_settle_skip_reason(0.8, cfg) == "too_far_for_new_entries"
  assert entry_too_far_for_manual_skip_reason(0.8, cfg) is None
  assert max_hours_to_settle_for_manual_entry(cfg) == 1.35


def test_manual_still_blocks_far_future_books():
  cfg = {"human_trading": {"max_hours_to_settle_for_entry": 1.35}}
  assert entry_too_far_for_manual_skip_reason(15.0, cfg) == "too_far_for_new_entries"
