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
  return reason in (
    "PROFIT TARGET",
    "PROFIT TRAIL",
    "TAKE PROFIT",
    "LEG TAKE PROFIT",
    "LEG TRAIL",
    "REASSESS NEUTRAL TP",
  )


@dataclass
class CheapLegExitConfig:
  max_entry_cents: int = 20
  cut_loss_cents: int = 10


def cheap_leg_exit_config(cfg: dict[str, Any] | None, *, kind: str) -> CheapLegExitConfig:
  """Read cheap-leg stop thresholds from hourly.bot or intra_slot.bot config."""
  bot_cfg: dict[str, Any] = {}
  if cfg:
    if kind == "hourly":
      bot_cfg = (cfg.get("hourly") or {}).get("bot") or {}
    else:
      bot_cfg = (cfg.get("intra_slot") or {}).get("bot") or {}
  return CheapLegExitConfig(
    max_entry_cents=int(bot_cfg.get("cheap_leg_max_entry_cents", 20)),
    cut_loss_cents=int(bot_cfg.get("cheap_leg_cut_loss_cents", 10)),
  )


def evaluate_cheap_leg_cut_loss(
  pos: dict[str, Any],
  mark_cents: int | None,
  cfg: CheapLegExitConfig,
) -> tuple[str | None, str]:
  """Tighter mark-based stop for low-cent entry legs (before normal CUT LOSSES)."""
  if mark_cents is None:
    return None, ""
  entry_c = int(pos.get("entry_price_cents") or 0)
  if entry_c <= 0 or entry_c > cfg.max_entry_cents:
    return None, ""
  if int(mark_cents) <= cfg.cut_loss_cents:
    return (
      "CHEAP LEG CUT LOSS",
      f"Cheap leg entry {entry_c}¢ — mark {int(mark_cents)}¢ at/below {cfg.cut_loss_cents}¢ floor",
    )
  return None, ""


@dataclass
class Slot15LegExitConfig:
  """Aggressive contract-mark exits for 15m bot (bird-in-the-hand)."""

  leg_take_profit_cents: int = 3
  leg_stop_loss_cents: int = 4
  leg_take_profit_usd: float = 0.10
  leg_trail_arm_usd: float = 0.10
  leg_trail_giveback_usd: float = 0.05
  leg_trail_giveback_pct: float = 0.30
  reassess_neutral_take_profit: bool = True
  reassess_neutral_band: float = 0.07
  reassess_neutral_min_unrealized_usd: float = 0.05


def slot15_leg_exit_config(cfg: dict[str, Any] | None) -> Slot15LegExitConfig:
  bot_cfg = (cfg.get("intra_slot") or {}).get("bot") or {} if cfg else {}
  return Slot15LegExitConfig(
    leg_take_profit_cents=int(bot_cfg.get("leg_take_profit_cents", 3)),
    leg_stop_loss_cents=int(bot_cfg.get("leg_stop_loss_cents", 4)),
    leg_take_profit_usd=float(bot_cfg.get("leg_take_profit_usd", 0.10)),
    leg_trail_arm_usd=float(bot_cfg.get("leg_trail_arm_usd", 0.10)),
    leg_trail_giveback_usd=float(bot_cfg.get("leg_trail_giveback_usd", 0.05)),
    leg_trail_giveback_pct=float(bot_cfg.get("leg_trail_giveback_pct", 0.30)),
    reassess_neutral_take_profit=bool(bot_cfg.get("reassess_neutral_take_profit", True)),
    reassess_neutral_band=float(bot_cfg.get("reassess_neutral_band", 0.07)),
    reassess_neutral_min_unrealized_usd=float(
      bot_cfg.get("reassess_neutral_min_unrealized_usd", 0.05)
    ),
  )


def mark_vs_entry_cents(pos: dict[str, Any], mark_cents: int | None) -> int | None:
  if mark_cents is None:
    return None
  entry_c = int(pos.get("entry_price_cents") or 0)
  if entry_c <= 0:
    return None
  return int(mark_cents) - entry_c


def evaluate_slot15_leg_stop_loss(
  pos: dict[str, Any],
  mark_cents: int | None,
  leg_cfg: Slot15LegExitConfig,
) -> tuple[str | None, str]:
  delta = mark_vs_entry_cents(pos, mark_cents)
  if delta is None or leg_cfg.leg_stop_loss_cents <= 0:
    return None, ""
  if delta <= -leg_cfg.leg_stop_loss_cents:
    entry_c = int(pos["entry_price_cents"])
    return (
      "LEG STOP",
      f"Mark {int(mark_cents)}¢ vs entry {entry_c}¢ ({delta:+d}¢) — leg stop −{leg_cfg.leg_stop_loss_cents}¢",
    )
  return None, ""


def evaluate_slot15_leg_take_profit(
  pos: dict[str, Any],
  mark_cents: int | None,
  unrealized_usd: float | None,
  leg_cfg: Slot15LegExitConfig,
) -> tuple[str | None, str]:
  delta = mark_vs_entry_cents(pos, mark_cents)
  cents_hit = (
    delta is not None
    and leg_cfg.leg_take_profit_cents > 0
    and delta >= leg_cfg.leg_take_profit_cents
  )
  usd_hit = (
    unrealized_usd is not None
    and leg_cfg.leg_take_profit_usd > 0
    and unrealized_usd >= leg_cfg.leg_take_profit_usd
  )
  if not cents_hit and not usd_hit:
    return None, ""
  entry_c = int(pos["entry_price_cents"])
  parts: list[str] = []
  if cents_hit and delta is not None:
    parts.append(f"mark +{delta}¢ (≥{leg_cfg.leg_take_profit_cents}¢)")
  if usd_hit and unrealized_usd is not None:
    parts.append(f"+${unrealized_usd:.2f} unrealized (≥${leg_cfg.leg_take_profit_usd:.2f})")
  return (
    "LEG TAKE PROFIT",
    f"Bird in hand — entry {entry_c}¢, mark {int(mark_cents)}¢: {' · '.join(parts)}",
  )


def _reassess_prob_is_neutral(prob_up: float | None, band: float) -> bool:
  if prob_up is None or band <= 0:
    return False
  return abs(float(prob_up) - 0.5) <= band


def evaluate_slot15_reassess_neutral_take_profit(
  pos: dict[str, Any],
  unrealized_usd: float | None,
  monitor: dict[str, Any],
  leg_cfg: Slot15LegExitConfig,
) -> tuple[str | None, str]:
  if not leg_cfg.reassess_neutral_take_profit:
    return None, ""
  if unrealized_usd is None or unrealized_usd < leg_cfg.reassess_neutral_min_unrealized_usd:
    return None, ""
  prob_up = monitor.get("reassessed_prob_up")
  if prob_up is None:
    return None, ""
  if not _reassess_prob_is_neutral(float(prob_up), leg_cfg.reassess_neutral_band):
    return None, ""
  summary = str(monitor.get("reassess_summary") or monitor.get("message") or "")
  up_pct = float(prob_up) * 100.0
  detail = (
    f"Green +${unrealized_usd:.2f} but reassess ~50/50 ({up_pct:.0f}% UP) — bank the gain"
  )
  if summary:
    detail += f" — {summary}"
  return "REASSESS NEUTRAL TP", detail


def evaluate_slot15_leg_trail_exit(
  unrealized_usd: float | None,
  peaks: dict[str, float],
  leg_cfg: Slot15LegExitConfig,
) -> tuple[str | None, str]:
  if unrealized_usd is None or unrealized_usd <= 0:
    return None, ""
  peak_usd = float(peaks.get("peak_unrealized_usd") or 0)
  if peak_usd < leg_cfg.leg_trail_arm_usd:
    return None, ""
  giveback_usd = peak_usd - unrealized_usd
  if leg_cfg.leg_trail_giveback_usd > 0 and giveback_usd >= leg_cfg.leg_trail_giveback_usd:
    return (
      "LEG TRAIL",
      f"Peak +${peak_usd:.2f} now +${unrealized_usd:.2f} — leg trail giveback ${giveback_usd:.2f}",
    )
  if leg_cfg.leg_trail_giveback_pct > 0:
    floor_usd = peak_usd * (1.0 - leg_cfg.leg_trail_giveback_pct)
    if unrealized_usd <= floor_usd:
      giveback_pct = max(0.0, giveback_usd / peak_usd) * 100.0
      return (
        "LEG TRAIL",
        f"Peak +${peak_usd:.2f} now +${unrealized_usd:.2f} — leg trail giveback {giveback_pct:.0f}%",
      )
  return None, ""


def evaluate_slot15_contract_exits(
  *,
  pos: dict[str, Any],
  mark_cents: int | None,
  unrealized_usd: float | None,
  monitor: dict[str, Any],
  peaks: dict[str, float],
  hold_seconds: float | None,
  settings: ProfitExitSettings | None,
  exit_ctx: AdaptiveExitContext,
  cfg: dict[str, Any] | None,
  include_monitor_fallback: bool = True,
  monitor_action: str | None = None,
  monitor_message: str | None = None,
  cut_loss_min_usd: float = 0.05,
) -> tuple[str | None, str]:
  """Contract-first exit chain for 15m bot; optional slot-monitor fallback last."""
  leg_cfg = slot15_leg_exit_config(cfg)

  reason, detail = evaluate_slot15_leg_stop_loss(pos, mark_cents, leg_cfg)
  if reason:
    return reason, detail

  reason, detail = evaluate_slot15_leg_take_profit(pos, mark_cents, unrealized_usd, leg_cfg)
  if reason:
    return reason, detail

  reason, detail = evaluate_slot15_reassess_neutral_take_profit(
    pos, unrealized_usd, monitor, leg_cfg,
  )
  if reason:
    return reason, detail

  cheap_cfg = cheap_leg_exit_config(cfg, kind="slot15")
  reason, detail = evaluate_cheap_leg_cut_loss(pos, mark_cents, cheap_cfg)
  if reason:
    return reason, detail

  reason, detail = evaluate_slot15_leg_trail_exit(unrealized_usd, peaks, leg_cfg)
  if reason:
    return reason, detail

  if settings:
    reason, detail = evaluate_adaptive_profit_exit(
      settings=settings,
      unrealized_usd=unrealized_usd,
      cost_usd=float(pos.get("cost_usd") or 0),
      peaks=peaks,
      hold_seconds=hold_seconds,
      ctx=exit_ctx,
    )
    if reason:
      return reason, detail

  if not include_monitor_fallback:
    return None, ""

  action = str(monitor_action if monitor_action is not None else monitor.get("action") or "")
  message = str(monitor_message if monitor_message is not None else monitor.get("message") or "")
  if action == "TAKE PROFIT" and unrealized_usd is not None and unrealized_usd > 0:
    return "TAKE PROFIT", message
  if action in ("CUT LOSS", "CUT LOSSES"):
    if unrealized_usd is None:
      return None, ""
    if unrealized_usd < -cut_loss_min_usd:
      return "CUT LOSSES", message
  return None, ""
