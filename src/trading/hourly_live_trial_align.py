"""Align live hourly bot behavior with paper hourly trial (exits, entries, execution)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from src.backtest.mechanics_profiles import is_hourly_trial_kind, live_mechanics_profile_for_cfg
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
  mirror_trial_stake_sizing: bool = True
  mirror_trial_scale_in: bool = True
  leg_stop_reentry_cooldown_seconds: int = 600
  leg_stop_event_cooldown_seconds: int = 300
  mirror_max_stake_per_entry_usd: float = 4.0
  mirror_max_budget_fraction_per_entry: float = 0.30
  mirror_max_contracts_per_entry: int = 8
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
    exits = dict(raw.get("exits") or {})
    stake = dict(raw.get("stake") or {})
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
      "mirror_trial_stake_sizing": bool(exe.get("mirror_trial_stake_sizing", True)),
      "mirror_trial_scale_in": bool(exe.get("mirror_trial_scale_in", True)),
      "block_scale_in_after_quick_exit_cut": bool(exe.get("block_scale_in_after_quick_exit_cut", True)),
      "leg_stop_reentry_cooldown_seconds": int(
        exits.get("leg_stop_reentry_cooldown_seconds", 600)
      ),
      "leg_stop_event_cooldown_seconds": int(
        exits.get("leg_stop_event_cooldown_seconds", 300)
      ),
      "mirror_max_stake_per_entry_usd": float(
        stake.get("max_stake_per_entry_usd", 4.0)
      ),
      "mirror_max_budget_fraction_per_entry": float(
        stake.get("max_budget_fraction_per_entry", 0.30)
      ),
      "mirror_max_contracts_per_entry": int(stake.get("max_contracts_per_entry", 8)),
      "compare_pair_window_seconds": int(cmp_.get("pair_window_seconds", 180)),
    }
    if "min_hold_seconds" in qx:
      kw["quick_exit_min_hold_seconds"] = int(qx["min_hold_seconds"])
    if "cut_loss_min_hold_seconds" in qx:
      kw["quick_exit_cut_loss_min_hold_seconds"] = int(qx["cut_loss_min_hold_seconds"])
    if "max_quick_exit_cuts_per_hour" in wh:
      kw["whipsaw_max_quick_exit_cuts_per_hour"] = int(wh["max_quick_exit_cuts_per_hour"])
    return replace(cls(), **kw)


def _live_hourly_align_enabled(cfg: dict[str, Any] | None, *, kind: str, mode: str) -> bool:
  if is_hourly_trial_kind(kind):
    return False
  if kind not in ("hourly", "slot15"):
    return False
  if str(mode).lower() != "live":
    return False
  return HourlyLiveTrialAlignConfig.from_cfg(cfg, kind=kind).enabled


def live_trial_align_active(cfg: dict[str, Any] | None, *, kind: str, mode: str) -> bool:
  """Entry/stake/inventory mirror to standard trial — off when live_mechanics_profile is set."""
  if not _live_hourly_align_enabled(cfg, kind=kind, mode=mode):
    return False
  return live_mechanics_profile_for_cfg(cfg) is None


def live_mech_paper_mirror_active(cfg: dict[str, Any] | None, *, kind: str, mode: str) -> bool:
  """BTC live hourly mirrors hourly_trial_mech (mechanical_fixes paper bot)."""
  if not _live_hourly_align_enabled(cfg, kind=kind, mode=mode):
    return False
  return live_mechanics_profile_for_cfg(cfg) == "mechanical_fixes"


def live_entry_stake_mirror_active(cfg: dict[str, Any] | None, *, kind: str, mode: str) -> bool:
  """Stake / scale-in / entry_strategy mirror — standard trial align or Mech paper mirror."""
  if live_trial_align_active(cfg, kind=kind, mode=mode):
    return True
  return live_mech_paper_mirror_active(cfg, kind=kind, mode=mode)


def live_entry_execution_mirror_active(cfg: dict[str, Any] | None, *, kind: str, mode: str) -> bool:
  """Cross-spread / ask-style execution mirror — standard trial align or Mech paper mirror."""
  if not (live_trial_align_active(cfg, kind=kind, mode=mode) or live_mech_paper_mirror_active(cfg, kind=kind, mode=mode)):
    return False
  return HourlyLiveTrialAlignConfig.from_cfg(cfg, kind=kind).mirror_trial_entry_execution


def live_trial_exit_align_active(cfg: dict[str, Any] | None, *, kind: str, mode: str) -> bool:
  """Trial leg exits + leg-stop cooldowns — stays on for live_mechanics_profile."""
  return _live_hourly_align_enabled(cfg, kind=kind, mode=mode)


def live_resting_entry_guards_active(cfg: dict[str, Any] | None, *, kind: str, mode: str) -> bool:
  """Live-only resting-limit guards (independent of trial entry mirror)."""
  return _live_hourly_align_enabled(cfg, kind=kind, mode=mode)


def skip_soft_rally_entry_overlay(cfg: dict[str, Any] | None, *, kind: str) -> bool:
  """Skip soft_rally overlay only for BTC Mech live (mirrors trial mech, not soft_rally trial)."""
  if kind != "hourly":
    return False
  return live_mechanics_profile_for_cfg(cfg) is not None


def skip_live_inventory_guards(cfg: dict[str, Any] | None, *, kind: str, mode: str) -> bool:
  """Skip live_inventory caps only for BTC Mech live (paper trial never applies them)."""
  if kind != "hourly" or str(mode).lower() != "live":
    return False
  return live_mechanics_profile_for_cfg(cfg) is not None


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
  if not live_trial_exit_align_active(cfg, kind=kind, mode=mode):
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
  mode: str = "live",
) -> Any:
  if not live_trial_align_active(cfg, kind=kind, mode=mode):
    return qcfg
  acfg = HourlyLiveTrialAlignConfig.from_cfg(cfg, kind=kind)
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
  mode: str = "live",
) -> Any:
  if not live_trial_align_active(cfg, kind=kind, mode=mode):
    return wcfg
  acfg = HourlyLiveTrialAlignConfig.from_cfg(cfg, kind=kind)
  if acfg.whipsaw_max_quick_exit_cuts_per_hour is None:
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
  if not live_entry_stake_mirror_active(cfg, kind=kind, mode=mode):
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
  if not live_entry_execution_mirror_active(cfg, kind=kind, mode=mode):
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
  if not live_resting_entry_guards_active(cfg, kind=kind, mode=mode):
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
  if not live_resting_entry_guards_active(cfg, kind=kind, mode=mode):
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


def should_mirror_trial_stake_sizing(
  cfg: dict[str, Any] | None,
  *,
  kind: str,
  mode: str,
) -> bool:
  if not live_entry_stake_mirror_active(cfg, kind=kind, mode=mode):
    return False
  return HourlyLiveTrialAlignConfig.from_cfg(cfg, kind=kind).mirror_trial_stake_sizing


def apply_mirror_trial_entry_estrat(
  estrat: Any,
  cfg: dict[str, Any] | None,
  *,
  kind: str,
  mode: str,
) -> Any:
  """Restore trial-like scale-in and stake caps when live_trial_align is active."""
  from dataclasses import replace

  from src.trading.entry_strategy import EntryStrategyConfig

  if not live_entry_stake_mirror_active(cfg, kind=kind, mode=mode):
    return estrat
  acfg = HourlyLiveTrialAlignConfig.from_cfg(cfg, kind=kind)
  bot = _bot_block(cfg, kind=kind)
  es = dict(bot.get("entry_strategy") or {})
  kw: dict[str, Any] = {}
  if acfg.mirror_trial_scale_in:
    kw["allow_scale_in"] = bool(es.get("allow_scale_in", True))
    kw["scale_in_max_legs_per_ticker"] = int(es.get("scale_in_max_legs_per_ticker", 4))
    kw["scale_in_min_unrealized_pnl_usd"] = float(
      es.get("scale_in_min_unrealized_pnl_usd", 0.05)
    )
    kw["scale_in_min_ask_edge_improvement_cents"] = float(
      es.get("scale_in_min_ask_edge_improvement_cents", 0.0)
    )
  if acfg.mirror_trial_stake_sizing:
    kw["max_stake_per_entry_usd"] = acfg.mirror_max_stake_per_entry_usd
    kw["max_budget_fraction_per_entry"] = acfg.mirror_max_budget_fraction_per_entry
    kw["max_contracts_per_entry"] = acfg.mirror_max_contracts_per_entry
  if not kw:
    return estrat
  if isinstance(estrat, EntryStrategyConfig):
    return replace(estrat, **kw)
  return estrat


def mirror_trial_live_contract_count(
  *,
  pick: dict[str, Any],
  side: str,
  stake_usd: float,
  price_cents: int,
  max_spend_per_hour_usd: float,
  estrat: Any,
  cfg: dict[str, Any] | None,
  kind: str,
  mode: str,
) -> int:
  """Match paper trial contract sizing for live entries when align is on."""
  from src.trading.entry_strategy import cap_live_entry_contracts
  from src.trading.paper_execution import paper_entry_fill

  if not should_mirror_trial_stake_sizing(cfg, kind=kind, mode=mode):
    count = max(0, int(stake_usd // (price_cents / 100.0))) if price_cents > 0 else 0
    return cap_live_entry_contracts(
      count=count,
      price_cents=price_cents,
      max_spend_per_hour_usd=max_spend_per_hour_usd,
      estrat=estrat,
    )

  preview = paper_entry_fill(
    pick=pick,
    side=side,
    remaining_budget_usd=stake_usd,
  )
  if preview.get("ok"):
    return int(preview.get("contracts") or 0)
  count = max(0, int(stake_usd // (price_cents / 100.0))) if price_cents > 0 else 0
  return cap_live_entry_contracts(
    count=count,
    price_cents=price_cents,
    max_spend_per_hour_usd=max_spend_per_hour_usd,
    estrat=estrat,
  )


def leg_stop_entry_blocked(
  store: Any,
  event_ticker: str,
  *,
  cfg: dict[str, Any] | None,
  kind: str = "hourly",
  mode: str = "live",
) -> str | None:
  if not live_trial_exit_align_active(cfg, kind=kind, mode=mode):
    return None
  fn = getattr(store, "is_in_leg_stop_event_cooldown", None)
  if not callable(fn):
    return None
  if fn(event_ticker):
    return "leg_stop_event_cooldown"
  return None
