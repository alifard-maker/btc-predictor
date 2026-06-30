"""Tests for live settlement index entry guard."""

from __future__ import annotations

from src.trading.bot_settlement_index_gate import (
  build_settlement_index_status,
  is_settlement_index_source,
  live_settlement_index_skip_reason,
)


def test_settlement_index_sources():
  assert is_settlement_index_source("brti_live")
  assert is_settlement_index_source("erti_live")
  assert not is_settlement_index_source("exchange_fallback")
  assert not is_settlement_index_source("exchange_live")


def test_paper_mode_never_blocked():
  tab = {"brti_live": 100.0, "brti_source": "exchange_fallback"}
  assert live_settlement_index_skip_reason(tab, cfg={}, mode="paper") is None


def test_live_blocked_on_exchange_fallback():
  tab = {"brti_live": 100.0, "brti_source": "exchange_fallback"}
  cfg = {"live_settlement_index": {"enabled": True, "require_for_live_entries": True}}
  reason = live_settlement_index_skip_reason(tab, cfg=cfg, mode="live")
  assert reason == "settlement_index_not_live:exchange_fallback"


def test_live_allowed_on_brti():
  tab = {"brti_live": 100_050.25, "brti_source": "brti_live"}
  cfg = {"live_settlement_index": {"enabled": True, "require_for_live_entries": True}}
  assert live_settlement_index_skip_reason(tab, cfg=cfg, mode="live") is None
  status = build_settlement_index_status(tab, cfg={"price_feed": {"settlement_reference": "BRTI"}})
  assert status["ok"] is True
  assert status["live_entries_allowed"] is True


def test_slot15_tab_monitor_source():
  tab = {
    "monitor": {"current_price": 2500.5, "current_price_source": "erti_live"},
    "brti_source": "erti_live",
    "brti_live": 2500.5,
  }
  cfg = {"live_settlement_index": {"enabled": True}, "kalshi": {"brti_index_id": "ETHUSD_RTI"}}
  assert live_settlement_index_skip_reason(tab, cfg=cfg, mode="live", asset="eth") is None


def test_live_blocked_when_index_missing():
  tab = {"monitor": {"current_price_source": "unavailable"}}
  cfg = {"live_settlement_index": {"enabled": True, "require_for_live_entries": True}}
  assert live_settlement_index_skip_reason(tab, cfg=cfg, mode="live") == "settlement_index_unavailable"
