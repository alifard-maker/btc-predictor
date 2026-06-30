"""Kalshi 15m slot settlement for period rollover exits."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd

from src.features.slots import slot_end
from src.trading.bot_period_rollover import resolve_rollover_exit_cents


def slot_period_settle_utc(slot_key: str) -> datetime | None:
  """When a 15m slot's market settles (slot end)."""
  try:
    start = pd.Timestamp(slot_key)
    if start.tzinfo is None:
      start = start.tz_localize("UTC")
    return slot_end(start).to_pydatetime().astimezone(timezone.utc)
  except (TypeError, ValueError):
    return None


def should_rollover_close_slot15_leg(
  pos: dict[str, Any],
  prev_slot_key: str,
  *,
  kalshi: Any | None = None,
  now: datetime | None = None,
) -> bool:
  """Only rollover-close 15m legs after the slot has settled on Kalshi."""
  if str(pos.get("event_ticker") or "") != str(prev_slot_key):
    return False
  now = now or datetime.now(timezone.utc)
  settle_at = slot_period_settle_utc(str(prev_slot_key))
  if settle_at is None:
    return False
  if now < settle_at:
    return False
  if kalshi and getattr(kalshi, "authenticated", False):
    try:
      settlement = kalshi.slot_settlement(pd.Timestamp(prev_slot_key))
    except Exception:
      settlement = None
    if settlement is not None and settlement.settled:
      return True
    # Past slot end but Kalshi not finalized yet — wait.
    if (now - settle_at).total_seconds() < 120:
      return False
  return now >= settle_at


def resolve_slot15_rollover_exit_cents(
  pos: dict[str, Any],
  *,
  kalshi: Any | None,
  slot_key: str,
  market_ticker: str | None,
  current_market_ticker: str | None,
  quote: dict[str, Any],
  yes_mid_cents: int | None,
  price_for_side: Any,
) -> tuple[int, str]:
  """Prefer Kalshi 15m binary settlement; fall back to market mark."""
  from src.trading.kalshi_leg_exit import slot15_binary_exit_cents

  ticker = str(pos.get("market_ticker") or market_ticker or "")
  side = str(pos.get("side") or "yes")
  if kalshi and ticker:
    cents, note = slot15_binary_exit_cents(
      kalshi,
      market_ticker=ticker,
      side=side,
      slot_key=str(slot_key),
    )
    if cents is not None:
      return int(cents), note

  market = resolve_rollover_exit_cents(
    pos,
    current_market_ticker=current_market_ticker,
    quote=quote,
    yes_mid_cents=yes_mid_cents,
    price_for_side=price_for_side,
  )
  return market, f"marked @ {market}¢ (slot not settled on Kalshi yet)"
