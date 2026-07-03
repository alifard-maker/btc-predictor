"""Hour momentum governor — adapts entry limits from in-hour performance."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

from src.trading.entry_strategy import EntryStrategyConfig
from src.trading.hourly_regime import LateEntryConfig, late_entry_config
from src.trading.live_regime_adaptive import adaptive_passive_config


class HourMomentumState(str, Enum):
  CONSERVATIVE = "conservative"
  NORMAL = "normal"
  PRESSING = "pressing"
  LOCKED = "locked"


@dataclass(frozen=True)
class HourMomentumLevel:
  max_entries_per_cycle: int
  stake_mult: float
  max_stake_per_entry_usd: float | None
  late_entry_min_ask_edge_cents: float
  block_late_entry: bool = False


_DEFAULT_LEVELS: dict[str, HourMomentumLevel] = {
  "conservative": HourMomentumLevel(2, 0.8, None, 18.0),
  "normal": HourMomentumLevel(4, 1.0, None, 15.0),
  "pressing": HourMomentumLevel(6, 1.0, 4.0, 12.0),
  "locked": HourMomentumLevel(2, 0.7, None, 20.0, block_late_entry=True),
}


@dataclass(frozen=True)
class HourMomentumConfig:
  enabled: bool = False
  losing_pnl_usd: float = -0.01
  choppy_min_exits: int = 2
  choppy_pnl_band_usd: float = 0.25
  profit_protect_pnl_usd: float | None = 0.75
  profit_lock_threshold_usd: float | None = None
  min_closed_wins_to_press: int = 1
  conservative: HourMomentumLevel = _DEFAULT_LEVELS["conservative"]
  normal: HourMomentumLevel = _DEFAULT_LEVELS["normal"]
  pressing: HourMomentumLevel = _DEFAULT_LEVELS["pressing"]
  locked: HourMomentumLevel = _DEFAULT_LEVELS["locked"]


@dataclass(frozen=True)
class HourMomentumContext:
  realized_pnl_usd: float
  unrealized_pnl_usd: float
  closed_wins: int
  closed_losses: int
  exit_count: int
  adaptive_mode: str
  primary_pick_edge: float | None = None


@dataclass(frozen=True)
class HourMomentumPolicy:
  state: HourMomentumState
  reasons: tuple[str, ...]
  max_entries_per_cycle: int
  stake_mult: float
  max_stake_per_entry_usd: float | None
  late_entry_min_ask_edge_cents: float
  block_late_entry: bool

  def to_dict(self) -> dict[str, Any]:
    return {
      "state": self.state.value,
      "reasons": list(self.reasons),
      "max_entries_per_cycle": self.max_entries_per_cycle,
      "stake_mult": self.stake_mult,
      "max_stake_per_entry_usd": self.max_stake_per_entry_usd,
      "late_entry_min_ask_edge_cents": self.late_entry_min_ask_edge_cents,
      "block_late_entry": self.block_late_entry,
      "realized_pnl_usd": None,
    }


def _level_from_raw(name: str, raw: dict[str, Any] | None) -> HourMomentumLevel:
  defaults = _DEFAULT_LEVELS[name]
  raw = raw or {}
  max_stake_raw = raw.get("max_stake_per_entry_usd", defaults.max_stake_per_entry_usd)
  return HourMomentumLevel(
    max_entries_per_cycle=int(raw.get("max_entries_per_cycle", defaults.max_entries_per_cycle)),
    stake_mult=float(raw.get("stake_mult", defaults.stake_mult)),
    max_stake_per_entry_usd=float(max_stake_raw) if max_stake_raw is not None else None,
    late_entry_min_ask_edge_cents=float(
      raw.get("late_entry_min_ask_edge_cents", defaults.late_entry_min_ask_edge_cents)
    ),
    block_late_entry=bool(raw.get("block_late_entry", defaults.block_late_entry)),
  )


def hour_momentum_config(cfg: dict[str, Any] | None) -> HourMomentumConfig:
  raw = dict(((cfg or {}).get("hourly") or {}).get("bot") or {}).get("hour_momentum") or {}
  if not raw:
    return HourMomentumConfig(enabled=False)
  acfg = adaptive_passive_config(cfg)
  profit_lock = raw.get("profit_lock_threshold_usd")
  if profit_lock is None:
    profit_lock = acfg.profit_lock_usd if acfg.enabled else 1.25
  profit_protect = raw.get("profit_protect_pnl_usd")
  if profit_protect is None and profit_lock is not None:
    profit_protect = round(float(profit_lock) * 0.6, 2)
  return HourMomentumConfig(
    enabled=bool(raw.get("enabled", False)),
    losing_pnl_usd=float(raw.get("losing_pnl_usd", -0.01)),
    choppy_min_exits=int(raw.get("choppy_min_exits", 2)),
    choppy_pnl_band_usd=float(raw.get("choppy_pnl_band_usd", 0.25)),
    profit_protect_pnl_usd=float(profit_protect) if profit_protect is not None else None,
    profit_lock_threshold_usd=float(profit_lock) if profit_lock is not None else None,
    min_closed_wins_to_press=int(raw.get("min_closed_wins_to_press", 1)),
    conservative=_level_from_raw("conservative", raw.get("conservative")),
    normal=_level_from_raw("normal", raw.get("normal")),
    pressing=_level_from_raw("pressing", raw.get("pressing")),
    locked=_level_from_raw("locked", raw.get("locked")),
  )


def _level_for_state(state: HourMomentumState, mcfg: HourMomentumConfig) -> HourMomentumLevel:
  if state == HourMomentumState.CONSERVATIVE:
    return mcfg.conservative
  if state == HourMomentumState.PRESSING:
    return mcfg.pressing
  if state == HourMomentumState.LOCKED:
    return mcfg.locked
  return mcfg.normal


def _classify_state(
  ctx: HourMomentumContext,
  mcfg: HourMomentumConfig,
) -> tuple[HourMomentumState, list[str]]:
  reasons: list[str] = []
  profit_protect = mcfg.profit_protect_pnl_usd
  if profit_protect is not None and ctx.realized_pnl_usd >= profit_protect:
    reasons.append(f"profit_protect>={profit_protect:.2f}")
    return HourMomentumState.LOCKED, reasons

  choppy = (
    ctx.exit_count >= mcfg.choppy_min_exits
    and abs(ctx.realized_pnl_usd) <= mcfg.choppy_pnl_band_usd
    and ctx.closed_wins > 0
    and ctx.closed_losses > 0
  )
  if ctx.realized_pnl_usd < mcfg.losing_pnl_usd:
    reasons.append(f"losing_pnl_{ctx.realized_pnl_usd:.2f}")
    return HourMomentumState.CONSERVATIVE, reasons
  if choppy:
    reasons.append("choppy_hour")
    return HourMomentumState.CONSERVATIVE, reasons

  can_press = (
    ctx.adaptive_mode == "rally"
    and ctx.realized_pnl_usd > 0
    and ctx.closed_wins >= mcfg.min_closed_wins_to_press
  )
  if can_press:
    reasons.append("rally_winning_hour")
    reasons.append(f"closed_wins_{ctx.closed_wins}")
    return HourMomentumState.PRESSING, reasons

  reasons.append("default_normal")
  return HourMomentumState.NORMAL, reasons


def compute_hour_momentum(
  ctx: HourMomentumContext,
  cfg: dict[str, Any] | None,
) -> HourMomentumPolicy | None:
  """Return momentum policy or None when disabled."""
  mcfg = hour_momentum_config(cfg)
  if not mcfg.enabled:
    return None

  state, reasons = _classify_state(ctx, mcfg)
  if ctx.adaptive_mode == "defense" and state == HourMomentumState.PRESSING:
    state = HourMomentumState.NORMAL
    reasons = ["defense_no_press", *reasons]

  level = _level_for_state(state, mcfg)
  return HourMomentumPolicy(
    state=state,
    reasons=tuple(reasons),
    max_entries_per_cycle=level.max_entries_per_cycle,
    stake_mult=level.stake_mult,
    max_stake_per_entry_usd=level.max_stake_per_entry_usd,
    late_entry_min_ask_edge_cents=level.late_entry_min_ask_edge_cents,
    block_late_entry=level.block_late_entry,
  )


def apply_hour_momentum_policy(
  estrat: EntryStrategyConfig,
  policy: HourMomentumPolicy | None,
) -> EntryStrategyConfig:
  if policy is None:
    return estrat
  max_entries = min(estrat.max_entries_per_cycle, policy.max_entries_per_cycle)
  max_stake = float(estrat.max_stake_per_entry_usd)
  if policy.max_stake_per_entry_usd is not None:
    max_stake = min(max_stake, float(policy.max_stake_per_entry_usd))
  elif policy.stake_mult != 1.0:
    max_stake = round(max_stake * policy.stake_mult, 2)
  min_kelly = float(estrat.min_kelly_stake_usd)
  if policy.stake_mult != 1.0:
    min_kelly = round(min_kelly * policy.stake_mult, 2)
  return replace(
    estrat,
    max_entries_per_cycle=max_entries,
    max_stake_per_entry_usd=max_stake,
    min_kelly_stake_usd=min_kelly,
  )


def resolve_late_entry_config(
  cfg: dict[str, Any] | None,
  policy: HourMomentumPolicy | None,
) -> LateEntryConfig:
  base = late_entry_config(cfg)
  if policy is None:
    return base
  if policy.block_late_entry:
    return replace(base, enabled=False)
  return replace(base, min_ask_edge_cents=policy.late_entry_min_ask_edge_cents)


def hour_momentum_payload(
  policy: HourMomentumPolicy | None,
  *,
  realized_pnl_usd: float,
  unrealized_pnl_usd: float,
) -> dict[str, Any] | None:
  if policy is None:
    return None
  out = policy.to_dict()
  out["realized_pnl_usd"] = round(realized_pnl_usd, 2)
  out["unrealized_pnl_usd"] = round(unrealized_pnl_usd, 2)
  return out
