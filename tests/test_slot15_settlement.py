"""Tests for slot15 settlement rollover guards."""

from __future__ import annotations

from datetime import datetime, timezone

from src.data.kalshi import KalshiSlotSettlement
from src.trading.slot15_settlement import (
  should_rollover_close_slot15_leg,
  slot_period_settle_utc,
)


class _KalshiSlotStub:
  authenticated = True

  def __init__(self, settlement):
    self._settlement = settlement

  def slot_settlement(self, _slot_start):
    return self._settlement


def test_slot_period_settle_utc():
  settle = slot_period_settle_utc("2026-06-30T11:15:00+00:00")
  assert settle is not None
  assert settle.minute in (30, 15)


def test_should_not_close_before_slot_end():
  pos = {"event_ticker": "2026-06-30T11:15:00+00:00", "market_ticker": "KXBTC15M-T"}
  now = datetime(2026, 6, 30, 11, 20, tzinfo=timezone.utc)
  assert not should_rollover_close_slot15_leg(pos, "2026-06-30T11:15:00+00:00", now=now)


def test_should_close_after_kalshi_settled():
  pos = {"event_ticker": "2026-06-30T11:00:00+00:00", "market_ticker": "KXBTC15M-T"}
  settlement = KalshiSlotSettlement(
    market_ticker="KXBTC15M-T",
    slot_open=datetime(2026, 6, 30, 11, 0, tzinfo=timezone.utc),
    slot_close=datetime(2026, 6, 30, 11, 15, tzinfo=timezone.utc),
    open_brti=59000.0,
    close_brti=59100.0,
    status="settled",
  )
  kalshi = _KalshiSlotStub(settlement)
  now = datetime(2026, 6, 30, 11, 16, tzinfo=timezone.utc)
  assert should_rollover_close_slot15_leg(
    pos, "2026-06-30T11:00:00+00:00", kalshi=kalshi, now=now,
  )
