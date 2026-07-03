"""Align live hourly bot behavior with paper hourly trial (exits, entries, execution)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from src.backtest.mechanics_profiles import is_hourly_trial_kind
from src.trading.live_entry_price import LiveEntryPricingConfig


def _bot_block(cfg: dict[str, Any] | None, *, kind: str) -> dict[str, Any]:
  if not cfg:
    return {}
  if kind == "slot15":
    return (cfg.get("intra_slot") or {}).get("bot") or {}
  return (cfg.get("hourly") or {}).get("bot") or {}


def _align_raw(cfg: dict[str, Any] | None, *, kind: str = "hourly") -> dict[str, Any]:
  return dict(_bot_block(cfg, kind=kind).get("live_trial_align") or {})


@dataclass(frozen=True)
class HourlyLiveTrialAlignConfig:
  enabled: bool = True
  live_exit_mode: str = "hybrid"
  hybrid_adaptive_modes: tuple[str, ...] = ("defense",)
  hybrid_momentum_states: tuple[str, ...] = ("conservative",)
  hybrid_max_hold_seconds: int = 600
  entry_align_with_trial: bool = True
  align_live_inventory: bool = True
  quick_exit_min_hold_seconds: int | None = 60
  quick_exit_cut_loss_min_hold_seconds: int | None = 60
  prefer_passive_below_edge_cents: float = 12.0
  mirror_trial_entry_execution: bool = True
  block_reentry_while_resting: bool = True
  block_scale_in_after_quick_exit_cut: bool = True
  compare_pair_window_seconds: int = 180
  whipsaw_max_quick_exit_cuts_per_hour: int | None = 2

  @classmethod
  def from_cfg(cls, cfg: dict[str, Any] | None, *, kind: str = "hourly") -> HourlyLiveTrialAlignConfig:
    raw = _align_raw(cfg, kind=kind)
    if not raw:
      return replace(cls(), enabled=False)
    hybrid = dict(raw.get("hybrid") or {})
    qx = dict(raw.get("quick_exit") or {})
    exe = dict(raw.get("execution") or {})
    cmp_ = dict(raw.get("compare") or {})
    wh = dict(raw.get("whipsaw") or {})
    modes = tuple(str(m).lower() for m in (hybrid.get("adaptive_modes") or ["defense"]))
    moms = tuple(str(m).lower() for m in (hybrid.get("hour_momentum_states") or ["conservative"]))
    kw: dict[str, Any] = {
      "enabled": bool(raw.get("enabled", True)),
      "live_exit_mode": str(raw.get("live_exit_mode") or "hybrid").lower(),
      "hybrid_adaptive_modes": modes,
      "hybrid_momentum_states": moms,
      "hybrid_max_hold_seconds": int(hybrid.get("max_hold_seconds", 600)),
      "entry_align_with_trial": bool(raw.get("entry_align_with_trial", True)),
      "align_live_inventory": bool(raw.get("align_live_inventory", True)),
      "prefer_passive_below_edge_cents": float(exe.get("prefer_passive_below_edge_cents", 12.0)),
      "mirror_trial_entry_execution": bool(exe.get("mirror_trial_entry_execution", True)),
      "block_reentry_while_resting": bool(exe.get("block_reentry_while_resting", True)),
      "block_scale_in_after_quick_exit_cut": bool(exe.get("block_scale_in_after_quick_exit_cut", True)),
      "compare_pair_window_seconds": int(cmp_.get("pair_window_seconds", 180)),
    }
    if "min_hold_seconds" in qx:
      kw["quick_exit_min_hold_seconds"] = int(qx["min_hold_seconds"])
    if "cut_loss_min_hold_seconds" in qx:
      kw["quick_exit_cut_loss_min_hold_seconds"] = int(qx["cut_loss_min_hold_seconds"])
    if "max_quick_exit_cuts_per_hour" in wh:
      kw["whipsaw_max_quick_exit_cuts_per_hour"] = int(wh["max_quick_exit_cuts_per_hour"])
    return replace(cls(), **kw)


def live_trial_align_active(cfg: dict[str, Any] | None, *, kind: str, mode: str) -> bool:
  if is_hourly_trial_kind(kind) or kind != "hourly":
    return False
  acfg = HourlyLiveTrialAlignConfig.from_cfg(cfg, kind=kind)
  return acfg.enabled and str(mode).lower() == "live"


def skip_soft_rally_entry_overlay(cfg: dict[str, Any] | None, *, kind: str) -> bool:
  acfg = HourlyLiveTrialAlignConfig.from_cfg(cfg, kind=kind)
  return acfg.enabled and acfg.entry_align_with_trial and kind == "hourly"


def skip_live_inventory_guards(cfg: dict[str, Any] | None, *, kind: str, mode: str) -> bool:
  acfg = HourlyLiveTrialAlignConfig.from_cfg(cfg, kind=kind)
  return acfg.enabled and acfg.align_live_inventory and kind == "hourly" and str(mode).lower() == "live"


def should_use_trial_leg_exits(
  cfg: dict[str, Any] | None,
  *,
  kind: str,
  mode: str,
  hold_seconds: float | None,
  adaptive_mode: str | None,
  hour_momentum_state: str | None,
) -> bool:
  if is_hourly_trial_kind(kind):
    return True
  if not live_trial_align_active(cfg, kind=kind, mode=mode):
    return False
  acfg = HourlyLiveTrialAlignConfig.from_cfg(cfg, kind=kind)
  exit_mode = acfg.live_exit_mode
  if exit_mode == "thesis":
    return False
  if exit_mode == "trial_legs":
    return True
  mode_l = str(adaptive_mode or "").lower()
  mom_l = str(hour_momentum_state or "").lower()
  if mode_l in acfg.hybrid_adaptive_modes:
    return True
  if mom_l in acfg.hybrid_momentum_states:
    return True
  if hold_seconds is not None and hold_seconds <= float(acfg.hybrid_max_hold_seconds):
    return True
  return False


def merge_quick_exit_align_overrides(
  qcfg: Any,
  cfg: dict[str, Any] | None,
  *,
  kind: str = "hourly",
) -> Any:
  acfg = HourlyLiveTrialAlignConfig.from_cfg(cfg, kind=kind)
  if not acfg.enabled:
    return qcfg
  kw: dict[str, Any] = {}
  if acfg.quick_exit_min_hold_seconds is not None:
    kw["min_hold_seconds"] = acfg.quick_exit_min_hold_seconds
  if acfg.quick_exit_cut_loss_min_hold_seconds is not None:
    kw["cut_loss_min_hold_seconds"] = acfg.quick_exit_cut_loss_min_hold_seconds
  if not kw:
    return qcfg
  return replace(qcfg, **kw)


def merge_whipsaw_align_overrides(
  wcfg: Any,
  cfg: dict[str, Any] | None,
  *,
  kind: str = "hourly",
) -> Any:
  acfg = HourlyLiveTrialAlignConfig.from_cfg(cfg, kind=kind)
  if not acfg.enabled or acfg.whipsaw_max_quick_exit_cuts_per_hour is None:
    return wcfg
  return replace(
    wcfg,
    max_quick_exit_cuts_per_hour=acfg.whipsaw_max_quick_exit_cuts_per_hour,
  )


def apply_align_entry_pricing(
  pricing: LiveEntryPricingConfig,
  pick: dict[str, Any],
  *,
  cfg: dict[str, Any] | None,
  kind: str = "hourly",
  mode: str,
) -> LiveEntryPricingConfig:
  if not live_trial_align_active(cfg, kind=kind, mode=mode):
    return pricing
  acfg = HourlyLiveTrialAlignConfig.from_cfg(cfg, kind=kind)
  if acfg.mirror_trial_entry_execution:
    return replace(pricing, cross_spread_enabled=True)
  try:
    edge = float(pick.get("ask_edge_cents") if pick.get("ask_edge_cents") is not None else pick.get("edge"))
  except (TypeError, ValueError):
    edge = None
  if edge is None:
    return pricing
  if edge < acfg.prefer_passive_below_edge_cents:
    return replace(pricing, cross_spread_enabled=False)
  return pricing


def should_mirror_trial_entry_execution(
  cfg: dict[str, Any] | None,
  *,
  kind: str,
  mode: str,
) -> bool:
  if not live_trial_align_active(cfg, kind=kind, mode=mode):
    return False
  return HourlyLiveTrialAlignConfig.from_cfg(cfg, kind=kind).mirror_trial_entry_execution


def pending_resting_enter_blocks_entry(
  store: Any,
  kalshi: Any,
  event_ticker: str,
  market_ticker: str,
  *,
  cfg: dict[str, Any] | None,
  kind: str = "hourly",
  mode: str = "live",
) -> str | None:
  """Return skip reason when an unfilled resting buy is still working this ticker."""
  if not live_trial_align_active(cfg, kind=kind, mode=mode):
    return None
  acfg = HourlyLiveTrialAlignConfig.from_cfg(cfg, kind=kind)
  if not acfg.block_reentry_while_resting:
    return None
  if not kalshi or not getattr(kalshi, "authenticated", False):
    return None
  latest = getattr(store, "latest_resting_enter", None)
  if not callable(latest):
    return None
  prior = latest(event_ticker, market_ticker, mode="live")
  if not prior:
    return None
  from src.trading.live_position_sync import order_still_resting

  oid = str(prior.get("kalshi_order_id") or "")
  if oid and order_still_resting(kalshi, oid):
    return f"pending_resting_limit:{market_ticker}"
  return None


def count_live_entry_slots_used(
  store: Any,
  kalshi: Any,
  event_ticker: str,
  open_positions: list[dict[str, Any]],
  *,
  cfg: dict[str, Any] | None,
  kind: str = "hourly",
  mode: str = "live",
) -> int:
  """Filled legs plus tickers with an active resting buy (pending position)."""
  open_tickers = {str(p.get("market_ticker") or "") for p in open_positions}
  open_tickers.discard("")
  slots = len(open_positions)
  if not live_trial_align_active(cfg, kind=kind, mode=mode):
    return slots
  acfg = HourlyLiveTrialAlignConfig.from_cfg(cfg, kind=kind)
  if not acfg.block_reentry_while_resting:
    return slots
  list_fn = getattr(store, "list_resting_enters", None)
  if not callable(list_fn) or not kalshi or not getattr(kalshi, "authenticated", False):
    return slots
  from src.trading.live_position_sync import order_still_resting

  for row in list_fn(event_ticker, mode="live"):
    ticker = str(row.get("market_ticker") or "")
    if not ticker or ticker in open_tickers:
      continue
    oid = str(row.get("kalshi_order_id") or "")
    if oid and order_still_resting(kalshi, oid):
      slots += 1
      open_tickers.add(ticker)
  return slots
