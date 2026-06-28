"""Shared profit-target and adaptive exit helpers for hourly and 15m auto-bet bots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol


class ProfitExitSettings(Protocol):
  take_profit_enabled: bool
  take_profit_mode: str
  take_profit_pct: float
  take_profit_usd: float
  min_hold_seconds: int
  trail_arm_profit_pct: float
  trail_giveback_pct: float
  trail_arm_profit_usd: float
  trail_giveback_usd: float
  min_take_profit_pct: float
  max_take_profit_pct: float


@dataclass
class AdaptiveExitContext:
  """Runtime context for scaling take-profit thresholds."""

  seconds_remaining: float | None = None
  period_seconds: float = 3600.0
  current_edge: float | None = None
  entry_edge: float | None = None
  regime_allow_trade: bool = True


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


def update_position_peaks(
  peaks: dict[str, float],
  unrealized_usd: float,
  cost_usd: float,
) -> dict[str, float]:
  """Update in-memory peak unrealized $ and % for a position."""
  pct = profit_pct(unrealized_usd, cost_usd) if unrealized_usd > 0 else 0.0
  peak_usd = max(float(peaks.get("peak_unrealized_usd") or 0), unrealized_usd)
  peak_pct = max(float(peaks.get("peak_profit_pct") or 0), pct)
  return {"peak_unrealized_usd": peak_usd, "peak_profit_pct": peak_pct}


def _hold_time_ok(min_hold_seconds: int, hold_seconds: float | None) -> bool:
  if min_hold_seconds <= 0:
    return True
  return hold_seconds is not None and hold_seconds >= float(min_hold_seconds)


def effective_take_profit_pct(
  settings: ProfitExitSettings,
  ctx: AdaptiveExitContext,
) -> float:
  """Scale base take-profit % by time, edge decay, and regime (adaptive/hybrid modes)."""
  base = float(settings.take_profit_pct)
  mode = str(settings.take_profit_mode or "hybrid").lower()
  if mode == "fixed":
    return base

  time_factor = 1.0
  if ctx.seconds_remaining is not None and ctx.period_seconds > 0:
    remaining_frac = max(0.0, min(1.0, ctx.seconds_remaining / ctx.period_seconds))
    time_factor = max(0.45, remaining_frac)

  edge_factor = 1.0
  entry_edge = ctx.entry_edge
  current_edge = ctx.current_edge
  if entry_edge is not None and entry_edge > 0 and current_edge is not None:
    edge_factor = max(0.5, min(1.0, current_edge / entry_edge))

  regime_factor = 0.75 if not ctx.regime_allow_trade else 1.0
  scaled = base * time_factor * edge_factor * regime_factor
  lo = float(settings.min_take_profit_pct)
  hi = float(settings.max_take_profit_pct)
  return max(lo, min(hi, scaled))


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
  if not _hold_time_ok(min_hold_seconds, hold_seconds):
    return False
  pct = profit_pct(unrealized_usd, cost_usd)
  if pct < take_profit_pct:
    return False
  if take_profit_usd > 0 and unrealized_usd < take_profit_usd:
    return False
  return True


def _trail_armed(
  peaks: dict[str, float],
  settings: ProfitExitSettings,
) -> bool:
  peak_usd = float(peaks.get("peak_unrealized_usd") or 0)
  peak_pct = float(peaks.get("peak_profit_pct") or 0)
  if peak_usd <= 0:
    return False
  if peak_pct >= float(settings.trail_arm_profit_pct):
    return True
  if float(settings.trail_arm_profit_usd) > 0 and peak_usd >= float(settings.trail_arm_profit_usd):
    return True
  return False


def should_trail_exit(
  *,
  enabled: bool,
  unrealized_usd: float | None,
  cost_usd: float,
  peaks: dict[str, float],
  settings: ProfitExitSettings,
  min_hold_seconds: int,
  hold_seconds: float | None,
) -> bool:
  """True when profit has faded from peak by configured giveback."""
  mode = str(settings.take_profit_mode or "hybrid").lower()
  if not enabled or mode not in ("trailing", "hybrid"):
    return False
  if unrealized_usd is None or unrealized_usd <= 0:
    return False
  if not _hold_time_ok(min_hold_seconds, hold_seconds):
    return False
  if not _trail_armed(peaks, settings):
    return False

  peak_usd = float(peaks.get("peak_unrealized_usd") or 0)
  if peak_usd <= 0:
    return False

  giveback_pct = float(settings.trail_giveback_pct)
  if giveback_pct > 0:
    floor_usd = peak_usd * (1.0 - giveback_pct)
    if unrealized_usd <= floor_usd:
      return True

  giveback_usd = float(settings.trail_giveback_usd)
  if giveback_usd > 0 and (peak_usd - unrealized_usd) >= giveback_usd:
    return True

  return False


def trail_giveback_pct_actual(peaks: dict[str, float], unrealized_usd: float) -> float:
  peak_usd = float(peaks.get("peak_unrealized_usd") or 0)
  if peak_usd <= 0:
    return 0.0
  return max(0.0, (peak_usd - unrealized_usd) / peak_usd) * 100.0


def profit_target_detail(unrealized_usd: float, cost_usd: float) -> str:
  pct = profit_pct(unrealized_usd, cost_usd) * 100.0
  return f"+{pct:.1f}% / +${unrealized_usd:.2f}"


def adaptive_profit_target_detail(
  unrealized_usd: float,
  cost_usd: float,
  effective_pct: float,
) -> str:
  pct = profit_pct(unrealized_usd, cost_usd) * 100.0
  return f"+{pct:.1f}% / +${unrealized_usd:.2f} (target {effective_pct * 100:.1f}%)"


def trail_exit_detail(
  peaks: dict[str, float],
  unrealized_usd: float,
) -> str:
  peak_usd = float(peaks.get("peak_unrealized_usd") or 0)
  giveback = trail_giveback_pct_actual(peaks, unrealized_usd)
  return f"peak +${peak_usd:.2f} now +${unrealized_usd:.2f} — giveback {giveback:.0f}%"


def evaluate_adaptive_profit_exit(
  *,
  settings: ProfitExitSettings,
  unrealized_usd: float | None,
  cost_usd: float,
  peaks: dict[str, float],
  hold_seconds: float | None,
  ctx: AdaptiveExitContext,
) -> tuple[str | None, str]:
  """Evaluate trailing and dynamic/fixed profit exits. Returns (reason, detail)."""
  if not settings.take_profit_enabled or unrealized_usd is None:
    return None, ""

  if should_trail_exit(
    enabled=settings.take_profit_enabled,
    unrealized_usd=unrealized_usd,
    cost_usd=cost_usd,
    peaks=peaks,
    settings=settings,
    min_hold_seconds=settings.min_hold_seconds,
    hold_seconds=hold_seconds,
  ):
    return "PROFIT TRAIL", trail_exit_detail(peaks, unrealized_usd)

  mode = str(settings.take_profit_mode or "hybrid").lower()
  if mode in ("fixed", "adaptive", "hybrid"):
    tp_pct = (
      effective_take_profit_pct(settings, ctx)
      if mode in ("adaptive", "hybrid")
      else float(settings.take_profit_pct)
    )
    if should_take_profit_target(
      enabled=True,
      unrealized_usd=unrealized_usd,
      cost_usd=cost_usd,
      take_profit_pct=tp_pct,
      take_profit_usd=float(settings.take_profit_usd),
      min_hold_seconds=settings.min_hold_seconds,
      hold_seconds=hold_seconds,
    ):
      if mode == "fixed":
        detail = profit_target_detail(unrealized_usd, cost_usd)
      else:
        detail = adaptive_profit_target_detail(unrealized_usd, cost_usd, tp_pct)
      return "PROFIT TARGET", detail

  return None, ""


def is_profit_exit_reason(reason: str | None) -> bool:
  return reason in ("PROFIT TARGET", "PROFIT TRAIL")
