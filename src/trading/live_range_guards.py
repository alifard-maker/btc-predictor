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


# Per-asset defaults (~0.1–0.18% of spot + σ term). Config overrides these per asset.
_ASSET_SPOT_GUARD_DEFAULTS: dict[str, dict[str, float]] = {
  "btc": {"min_buffer_usd": 75.0, "sigma_buffer_fraction": 0.20, "min_spot_pct_buffer": 0.12},
  "eth": {"min_buffer_usd": 12.0, "sigma_buffer_fraction": 0.22, "min_spot_pct_buffer": 0.18},
  "spx": {"min_buffer_usd": 25.0, "sigma_buffer_fraction": 0.18, "min_spot_pct_buffer": 0.10},
  "ndx": {"min_buffer_usd": 90.0, "sigma_buffer_fraction": 0.18, "min_spot_pct_buffer": 0.10},
}


def _asset_from_cfg(cfg: dict[str, Any] | None, asset: str | None = None) -> str:
  if asset:
    return str(asset).lower()
  if cfg and cfg.get("_asset"):
    return str(cfg["_asset"]).lower()
  return "btc"


def _range_band_spot_guard_cfg(
  cfg: dict[str, Any] | None,
  *,
  kind: str,
  asset: str | None = None,
) -> dict[str, Any]:
  asset_l = _asset_from_cfg(cfg, asset)
  defaults = dict(_ASSET_SPOT_GUARD_DEFAULTS.get(asset_l, _ASSET_SPOT_GUARD_DEFAULTS["btc"]))
  inv = _live_inventory_cfg(cfg, kind=kind)
  raw = dict(inv.get("range_band_spot_entry_guard") or {})
  by_asset = raw.pop("by_asset", None) or inv.get("range_band_spot_entry_guard_by_asset") or {}
  if isinstance(by_asset, dict):
    asset_raw = by_asset.get(asset_l)
    if isinstance(asset_raw, dict):
      raw = {**raw, **asset_raw}
  enabled = raw.get("enabled")
  if enabled is None:
    enabled = inv.get("range_band_spot_guard_enabled", True)
  return {
    "enabled": bool(enabled),
    "min_buffer_usd": float(
      raw.get("min_buffer_usd", inv.get("range_band_spot_min_buffer_usd", defaults["min_buffer_usd"])),
    ),
    "sigma_buffer_fraction": float(
      raw.get(
        "sigma_buffer_fraction",
        inv.get("range_band_spot_sigma_fraction", defaults["sigma_buffer_fraction"]),
      ),
    ),
    "min_spot_pct_buffer": float(
      raw.get("min_spot_pct_buffer", defaults["min_spot_pct_buffer"]),
    ),
    "asset": asset_l,
  }


def range_band_spot_entry_buffer_usd(
  *,
  spot_price: float,
  terminal_sigma: float | None,
  cfg: dict[str, Any] | None,
  kind: str = "hourly",
  asset: str | None = None,
) -> float:
  """Effective USD buffer for range-band spot entry guard."""
  gcfg = _range_band_spot_guard_cfg(cfg, kind=kind, asset=asset)
  buffer = float(gcfg["min_buffer_usd"])
  pct = float(gcfg["min_spot_pct_buffer"])
  if pct > 0 and spot_price > 0:
    buffer = max(buffer, spot_price * pct / 100.0)
  if terminal_sigma is not None and terminal_sigma > 0:
    buffer = max(buffer, float(terminal_sigma) * float(gcfg["sigma_buffer_fraction"]))
  return round(buffer, 2)


def range_band_spot_entry_block_reason(
  *,
  pick: dict[str, Any] | None,
  side: str,
  spot_price: float | None,
  terminal_sigma: float | None = None,
  cfg: dict[str, Any] | None,
  kind: str = "hourly",
  asset: str | None = None,
) -> str | None:
  """Block range-band entries when live spot hasn't reached the band (μ-only edge is misleading).

  YES: spot must be within buffer of band floor (or inside band).
  NO: spot must be within buffer of band cap (or outside band the other way).
  """
  if not is_range_pick(pick):
    return None
  gcfg = _range_band_spot_guard_cfg(cfg, kind=kind, asset=asset)
  if not gcfg["enabled"] or spot_price is None:
    return None
  floor = (pick or {}).get("floor_strike")
  cap = (pick or {}).get("cap_strike")
  if floor is None or cap is None:
    return None
  try:
    floor_f = float(floor)
    cap_f = float(cap)
    spot = float(spot_price)
  except (TypeError, ValueError):
    return None

  sigma: float | None = None
  if terminal_sigma is not None:
    try:
      sigma = float(terminal_sigma)
    except (TypeError, ValueError):
      sigma = None

  buffer = range_band_spot_entry_buffer_usd(
    spot_price=spot,
    terminal_sigma=sigma,
    cfg=cfg,
    kind=kind,
    asset=asset or gcfg.get("asset"),
  )

  side_l = str(side).lower()
  if side_l == "yes":
    if spot + buffer < floor_f:
      gap = floor_f - spot
      return f"range_band_spot_below_floor:{gap:.0f}>{buffer:.0f}"
  elif side_l == "no":
    if spot - buffer > cap_f:
      gap = spot - cap_f
      return f"range_band_spot_above_cap:{gap:.0f}>{buffer:.0f}"
  return None


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
