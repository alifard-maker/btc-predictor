"""Live-only guards for hourly range-band (Strategy 2) position sizing."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from src.trading.entry_strategy import EntryStrategyConfig
from src.trading.live_inventory_guards import _live_inventory_cfg


def is_range_market_ticker(market_ticker: str | None) -> bool:
  return bool(market_ticker and re.search(r"-B\d", str(market_ticker), re.I))


def is_range_pick(pick: dict[str, Any] | None) -> bool:
  if not pick:
    return False
  if str(pick.get("strike_type") or "").lower() == "between":
    return True
  if str(pick.get("contract_type") or "").lower() == "range":
    return True
  return is_range_market_ticker(str(pick.get("ticker") or ""))


def _leg_contracts(pos: dict[str, Any]) -> float:
  from src.trading.live_position_sync import _position_contracts

  return float(_position_contracts(pos))


def open_range_contracts_on_band(
  store: Any,
  event_ticker: str,
  market_ticker: str,
  side: str,
  open_positions: list[dict[str, Any]],
  *,
  mode: str = "live",
) -> float:
  """Sum open + resting-enter contracts for one band ticker/side this hour."""
  total = 0.0
  side_l = str(side).lower()
  for pos in open_positions:
    if pos.get("market_ticker") != market_ticker:
      continue
    if str(pos.get("side") or "").lower() != side_l:
      continue
    total += _leg_contracts(pos)
  list_fn = getattr(store, "list_resting_enters", None)
  if callable(list_fn):
    for row in list_fn(event_ticker, mode=mode):
      if row.get("market_ticker") != market_ticker:
        continue
      if str(row.get("side") or "").lower() != side_l:
        continue
      try:
        total += float(row.get("contracts") or 0)
      except (TypeError, ValueError):
        pass
  return round(total, 2)


def max_contracts_per_range_band_per_hour(
  cfg: dict[str, Any] | None,
  *,
  kind: str = "hourly",
) -> int | None:
  inv = _live_inventory_cfg(cfg, kind=kind)
  raw = inv.get("max_contracts_per_range_band_per_hour")
  if raw is None:
    return None
  cap = int(raw)
  return cap if cap > 0 else None


def range_band_hour_cap_block_reason(
  *,
  store: Any,
  event_ticker: str,
  market_ticker: str,
  side: str,
  open_positions: list[dict[str, Any]],
  cfg: dict[str, Any] | None,
  kind: str = "hourly",
  additional_contracts: int = 0,
  pick: dict[str, Any] | None = None,
) -> str | None:
  if pick is not None and not is_range_pick(pick):
    return None
  if pick is None and not is_range_market_ticker(market_ticker):
    return None
  cap = max_contracts_per_range_band_per_hour(cfg, kind=kind)
  if cap is None:
    return None
  used = open_range_contracts_on_band(
    store, event_ticker, market_ticker, side, open_positions,
  )
  if used + max(0, int(additional_contracts)) > cap:
    return (
      f"range_band_hour_contract_cap:{market_ticker}:"
      f"{used:.0f}+{additional_contracts}>{cap}"
    )
  return None


def clamp_range_band_hour_contracts(
  count: int,
  contracts_fp: float,
  *,
  store: Any,
  event_ticker: str,
  market_ticker: str,
  side: str,
  open_positions: list[dict[str, Any]],
  cfg: dict[str, Any] | None,
  kind: str = "hourly",
) -> tuple[int, float]:
  cap = max_contracts_per_range_band_per_hour(cfg, kind=kind)
  if cap is None or not is_range_market_ticker(market_ticker):
    return count, contracts_fp
  used = open_range_contracts_on_band(
    store, event_ticker, market_ticker, side, open_positions,
  )
  room = max(0.0, float(cap) - used)
  if room < 0.05:
    return 0, 0.0
  capped_fp = min(float(contracts_fp), room)
  capped = min(int(count), max(0, int(capped_fp)))
  if capped <= 0:
    return 0, 0.0
  return capped, round(capped_fp, 2)


def estrat_for_range_scale_in(
  estrat: EntryStrategyConfig,
  pick: dict[str, Any],
  cfg: dict[str, Any] | None,
  *,
  kind: str = "hourly",
  mode: str,
) -> EntryStrategyConfig:
  """Tighter scale-in on range bands in live (applies even when trial-align skips inventory)."""
  if str(mode).lower() != "live" or not is_range_pick(pick):
    return estrat
  inv = _live_inventory_cfg(cfg, kind=kind)
  kw: dict[str, Any] = {}
  if "allow_scale_in_range" in inv:
    kw["allow_scale_in"] = bool(inv["allow_scale_in_range"])
  if "scale_in_max_legs_per_ticker_range" in inv:
    kw["scale_in_max_legs_per_ticker"] = int(inv["scale_in_max_legs_per_ticker_range"])
  return replace(estrat, **kw) if kw else estrat


def apply_range_adoption_hour_cap(
  contracts: int,
  contracts_fp: float,
  *,
  store: Any,
  event_ticker: str,
  market_ticker: str,
  side: str,
  cfg: dict[str, Any] | None,
  kind: str = "hourly",
) -> tuple[int, float]:
  if not is_range_market_ticker(market_ticker):
    return contracts, contracts_fp
  open_fn = getattr(store, "open_positions", None)
  open_positions = list(open_fn(event_ticker)) if callable(open_fn) else []
  return clamp_range_band_hour_contracts(
    contracts,
    contracts_fp,
    store=store,
    event_ticker=event_ticker,
    market_ticker=market_ticker,
    side=side,
    open_positions=open_positions,
    cfg=cfg,
    kind=kind,
  )
