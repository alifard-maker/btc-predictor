"""Paper signal-mirror mode — bot enters/exits like dashboard manual lane."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from src.trading.eth_paper_experiment import eth_bot_cfg
from src.trading.entry_strategy import EntryStrategyConfig


def _hourly_bot_block(cfg: dict[str, Any] | None) -> dict[str, Any]:
  return dict(((cfg or {}).get("hourly") or {}).get("bot") or {})


def signal_mirror_cfg(
  cfg: dict[str, Any] | None,
  *,
  kind: str,
  asset: str,
) -> dict[str, Any]:
  """Resolve signal_mirror yaml for this bot kind."""
  asset = str(asset or "btc").lower()
  kind = str(kind or "hourly")
  if kind == "hourly_trial_mech":
    return dict(_hourly_bot_block(cfg).get("trial_mech") or {}).get("signal_mirror") or {}
  if asset == "eth" and kind == "hourly":
    return dict(eth_bot_cfg(cfg).get("signal_mirror") or {})
  return dict(_hourly_bot_block(cfg).get("signal_mirror") or {})


def signal_mirror_active(
  cfg: dict[str, Any] | None,
  *,
  kind: str,
  asset: str,
  mode: str,
) -> bool:
  if str(mode).lower() != "paper":
    return False
  return bool(signal_mirror_cfg(cfg, kind=kind, asset=asset).get("enabled"))


def signal_mirror_uses_thesis_exits(
  cfg: dict[str, Any] | None,
  *,
  kind: str,
  asset: str,
  mode: str,
) -> bool:
  if not signal_mirror_active(cfg, kind=kind, asset=asset, mode=mode):
    return False
  mcfg = signal_mirror_cfg(cfg, kind=kind, asset=asset)
  return bool(mcfg.get("use_thesis_exits", True))


def signal_mirror_skip_spot_guards(
  cfg: dict[str, Any] | None,
  *,
  kind: str,
  asset: str,
) -> bool:
  mcfg = signal_mirror_cfg(cfg, kind=kind, asset=asset)
  return bool(mcfg.get("skip_spot_guards", True))


def signal_mirror_entry_cfg(
  cfg: dict[str, Any] | None,
  mcfg: dict[str, Any],
) -> dict[str, Any]:
  """Overlay settle windows so mirror bot shares manual-lane hour bounds."""
  import copy

  out = copy.deepcopy(cfg or {})
  hourly = dict(out.get("hourly") or {})
  bot = dict(hourly.get("bot") or {})
  pf = (out.get("pnl_first") or {})
  max_h = mcfg.get("max_hours_to_settle_for_entry", pf.get("max_hours_to_settle_for_entry", 1.35))
  min_h = mcfg.get("min_hours_to_settle_for_entry", bot.get("min_hours_to_settle_for_entry"))
  bot["max_hours_to_settle_for_entry"] = float(max_h)
  if min_h is not None:
    bot["min_hours_to_settle_for_entry"] = float(min_h)
  late = dict(bot.get("late_entry") or {})
  late["enabled"] = bool(mcfg.get("late_entry_enabled", False))
  bot["late_entry"] = late
  hourly["bot"] = bot
  out["hourly"] = hourly
  return out


def apply_signal_mirror_entry_estrat(
  estrat: EntryStrategyConfig,
  mcfg: dict[str, Any],
) -> EntryStrategyConfig:
  """Relax entry strategy to mirror manual lane (actionable BUY, modest stake)."""
  kw: dict[str, Any] = {
    "min_ask_edge_cents": float(mcfg.get("min_ask_edge_cents", 0)),
    "max_entries_per_cycle": int(mcfg.get("max_entries_per_cycle", 4)),
    "max_concurrent_positions": int(mcfg.get("max_concurrent_positions", 20)),
    "max_stake_per_entry_usd": float(mcfg.get("max_stake_per_entry_usd", 2.50)),
    "max_contracts_per_entry": int(mcfg.get("max_contracts_per_entry", 6)),
    "allow_scale_in": bool(mcfg.get("allow_scale_in", False)),
    "tail_entry_block": False,
  }
  return replace(estrat, **kw)
