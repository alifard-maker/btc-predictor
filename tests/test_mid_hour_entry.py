"""Tests for optional mid-hour entry window gate."""

from src.trading.hourly_regime import mid_hour_entry_skip_reason


def test_mid_hour_disabled_no_skip():
  cfg = {"pnl_first": {"mid_hour_entry": {"enabled": False}}}
  assert mid_hour_entry_skip_reason(0.5, cfg) is None


def test_mid_hour_global_enabled_applies_to_btc():
  cfg = {"pnl_first": {"mid_hour_entry": {"enabled": True}}}
  assert mid_hour_entry_skip_reason(0.9, cfg, asset="btc", mode="live") == "mid_hour_too_early_for_entry"
  assert mid_hour_entry_skip_reason(0.35, cfg, asset="btc", mode="live") is None


def test_eth_enabled_for_live_and_paper():
  cfg = {"pnl_first": {"mid_hour_entry": {"enabled": False, "eth_enabled": True}}}
  assert mid_hour_entry_skip_reason(0.9, cfg, asset="eth", mode="live") == "mid_hour_too_early_for_entry"
  assert mid_hour_entry_skip_reason(0.35, cfg, asset="eth", mode="paper") is None


def test_eth_paper_enabled_without_global_flag():
  cfg = {"pnl_first": {"mid_hour_entry": {"enabled": False, "eth_paper_enabled": True}}}
  assert mid_hour_entry_skip_reason(0.9, cfg, asset="eth", mode="paper") == "mid_hour_too_early_for_entry"
  assert mid_hour_entry_skip_reason(0.9, cfg, asset="btc", mode="paper") is None


def test_mid_hour_blocks_too_early():
  cfg = {"pnl_first": {"mid_hour_entry": {"enabled": True}}}
  assert mid_hour_entry_skip_reason(0.9, cfg) == "mid_hour_too_early_for_entry"


def test_mid_hour_blocks_too_late():
  cfg = {"pnl_first": {"mid_hour_entry": {"enabled": True}}}
  assert mid_hour_entry_skip_reason(0.15, cfg) == "mid_hour_too_late_for_entry"


def test_mid_hour_allows_window():
  cfg = {"pnl_first": {"mid_hour_entry": {"enabled": True}}}
  assert mid_hour_entry_skip_reason(0.35, cfg) is None
