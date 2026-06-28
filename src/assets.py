"""Asset-specific config for multi-asset hourly prediction."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

SUPPORTED_ASSETS = ("btc", "eth")
DEFAULT_ASSET = "btc"


def asset_enabled(base_cfg: dict[str, Any], asset: str) -> bool:
  asset = asset.lower()
  if asset == DEFAULT_ASSET:
    return True
  block = base_cfg.get(asset) or {}
  return bool(block.get("enabled", True))


def asset_cfg(base_cfg: dict[str, Any], asset: str) -> dict[str, Any]:
  """Return a cfg dict scoped to *asset* (btc uses base config as-is)."""
  asset = asset.lower()
  if asset == DEFAULT_ASSET:
    out = copy.deepcopy(base_cfg)
    out["_asset"] = DEFAULT_ASSET
    return out

  block = base_cfg.get(asset) or {}
  if not block.get("enabled", True):
    raise ValueError(f"Asset {asset} is disabled in config")

  cfg = copy.deepcopy(base_cfg)
  data_root = Path(base_cfg["paths"]["logs"]).parent
  cfg["paths"] = {
    **base_cfg["paths"],
    "candles": str(data_root / asset / "candles"),
    "models": str(data_root / asset / "models"),
    "logs": str(data_root / asset / "logs"),
  }

  if sym := block.get("symbol"):
    cfg["symbol"] = sym
  if ex := block.get("exchange"):
    cfg["exchange"] = ex
  if fallbacks := block.get("exchange_fallbacks"):
    cfg["exchange_fallbacks"] = list(fallbacks)

  cfg["price_feed"] = {**base_cfg.get("price_feed", {}), **block.get("price_feed", {})}
  cfg["kalshi"] = {**base_cfg.get("kalshi", {}), **block.get("kalshi", {})}
  cfg["daily"] = {**base_cfg.get("daily", {}), **block.get("daily", {})}
  cfg["hourly"] = {**base_cfg.get("hourly", {}), **block.get("hourly", {})}
  cfg["paths"]["db"] = str(Path(cfg["paths"]["logs"]) / "predictions.db")
  cfg["_asset"] = asset
  return cfg


def index_id_for_cfg(cfg: dict[str, Any]) -> str:
  pf = cfg.get("price_feed") or {}
  return str(pf.get("index_id") or cfg.get("kalshi", {}).get("brti_index_id", "BRTI"))
