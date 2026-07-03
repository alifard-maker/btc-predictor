"""Asset-specific config for multi-asset hourly prediction."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

SUPPORTED_ASSETS = ("btc", "eth")
DEFAULT_ASSET = "btc"


def _deep_merge_dict(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
  """Merge overlay onto base; nested dicts are merged recursively."""
  out = dict(base)
  for key, value in overlay.items():
    if key in out and isinstance(out[key], dict) and isinstance(value, dict):
      out[key] = _deep_merge_dict(out[key], value)
    else:
      out[key] = value
  return out


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
  base_hourly = base_cfg.get("hourly") or {}
  block_hourly = block.get("hourly") or {}
  merged_hourly = {**base_hourly, **block_hourly}
  if "bot" in base_hourly or "bot" in block_hourly:
    merged_hourly["bot"] = _deep_merge_dict(
      base_hourly.get("bot") or {},
      block_hourly.get("bot") or {},
    )
  cfg["hourly"] = merged_hourly
  cfg["paths"]["db"] = str(Path(cfg["paths"]["logs"]) / "predictions.db")
  cfg["_asset"] = asset
  return cfg


def asset_v2_enabled(base_cfg: dict[str, Any], asset: str) -> bool:
  asset = asset.lower()
  if not asset_enabled(base_cfg, asset):
    return False
  if asset == DEFAULT_ASSET:
    return bool((base_cfg.get("hourly_v2") or {}).get("enabled", False))
  block = base_cfg.get(asset) or {}
  return bool((block.get("hourly_v2") or {}).get("enabled", False))


def asset_v2_cfg(base_cfg: dict[str, Any], asset: str) -> dict[str, Any]:
  """Scoped cfg for hourly v2 (path memory) — separate logs/models, shared Kalshi series."""
  cfg = asset_cfg(base_cfg, asset)
  v2 = base_cfg.get("hourly_v2") or {}
  if asset != DEFAULT_ASSET:
    v2 = {**v2, **((base_cfg.get(asset) or {}).get("hourly_v2") or {})}
  cfg["hourly_v2"] = v2
  cfg["_hourly_v2"] = True
  return cfg


def asset_v2_runtime_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
  """Overlay hourly_v2.bot onto hourly.bot so HourlyBot reads v2 settings without touching v1."""
  import copy

  c = copy.deepcopy(cfg)
  v2 = c.get("hourly_v2") or {}
  hourly = c.setdefault("hourly", {})
  for key in ("regime", "intrahour", "blend"):
    if key in v2 and isinstance(v2[key], dict):
      hourly[key] = {**hourly.get(key, {}), **v2[key]}
  if "bot" in v2:
    hourly["bot"] = {**hourly.get("bot", {}), **v2["bot"]}
  return c


def index_id_for_cfg(cfg: dict[str, Any]) -> str:
  pf = cfg.get("price_feed") or {}
  return str(pf.get("index_id") or cfg.get("kalshi", {}).get("brti_index_id", "BRTI"))
