"""Shared profit-target exit helpers for hourly and 15m auto-bet bots."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def position_hold_seconds(pos: dict[str, Any]) -> float | None:
  """Seconds since position was opened, or None if opened_at is missing."""
  opened = pos.get("opened_at")
  if not opened:
    return None
  opened_at = datetime.fromisoformat(str(opened).replace("Z", "+00:00"))
  if opened_at.tzinfo is None:
    opened_at = opened_at.replace(tzinfo=timezone.utc)
  return (datetime.now(timezone.utc) - opened_at).total_seconds()


def profit_pct(unrealized_usd: float, cost_usd: float) -> float:
  if cost_usd <= 0:
    return 0.0
  return unrealized_usd / cost_usd


def should_take_profit_target(
  *,
  enabled: bool,
  unrealized_usd: float | None,
  cost_usd: float,
  take_profit_pct: float,
  take_profit_usd: float,
  min_hold_seconds: int,
  hold_seconds: float | None,
) -> bool:
  """True when unrealized gain meets configured % and optional $ thresholds."""
  if not enabled or unrealized_usd is None:
    return False
  if unrealized_usd <= 0:
    return False
  if min_hold_seconds > 0:
    if hold_seconds is None or hold_seconds < float(min_hold_seconds):
      return False
  pct = profit_pct(unrealized_usd, cost_usd)
  if pct < take_profit_pct:
    return False
  if take_profit_usd > 0 and unrealized_usd < take_profit_usd:
    return False
  return True


def profit_target_detail(unrealized_usd: float, cost_usd: float) -> str:
  pct = profit_pct(unrealized_usd, cost_usd) * 100.0
  return f"+{pct:.1f}% / +${unrealized_usd:.2f}"
