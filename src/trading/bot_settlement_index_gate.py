"""Require CF Benchmarks BRTI/ERTI for live bot entries (not exchange fallback)."""

from __future__ import annotations

from typing import Any

from src.assets import index_id_for_cfg

SETTLEMENT_INDEX_SOURCES = frozenset({"brti_live", "erti_live"})
SKIP_SETTLEMENT_INDEX_UNAVAILABLE = "settlement_index_unavailable"
SKIP_SETTLEMENT_INDEX_NOT_LIVE_PREFIX = "settlement_index_not_live:"


def live_settlement_index_cfg(cfg: dict[str, Any] | None) -> dict[str, Any]:
  raw = (cfg or {}).get("live_settlement_index") or {}
  return {
    "enabled": bool(raw.get("enabled", True)),
    "require_for_live_entries": bool(raw.get("require_for_live_entries", True)),
  }


def is_settlement_index_source(source: str | None) -> bool:
  return str(source or "").lower() in SETTLEMENT_INDEX_SOURCES


def settlement_index_quote_from_tab(
  tab: dict[str, Any] | None,
  *,
  asset: str = "btc",
) -> tuple[float | None, str | None]:
  """Extract settlement index price + source from hourly or 15m tab payload."""
  del asset  # reserved for future per-asset validation
  if not tab:
    return None, None
  live = tab.get("live") or {}
  monitor = tab.get("monitor") or {}
  price_raw = (
    tab.get("brti_live")
    or live.get("brti_live")
    or live.get("current_price")
    or monitor.get("current_price")
  )
  source = (
    tab.get("brti_source")
    or live.get("brti_source")
    or live.get("current_price_source")
    or monitor.get("current_price_source")
  )
  if price_raw is None:
    return None, source
  try:
    return float(price_raw), source
  except (TypeError, ValueError):
    return None, source


def build_settlement_index_status(
  tab: dict[str, Any] | None,
  *,
  cfg: dict[str, Any] | None,
  price: float | None = None,
  source: str | None = None,
) -> dict[str, Any]:
  index_id = index_id_for_cfg(cfg or {})
  if price is None and source is None:
    price, source = settlement_index_quote_from_tab(tab)
  ok = price is not None and is_settlement_index_source(source)
  return {
    "index_id": index_id,
    "ok": ok,
    "source": source,
    "price": round(float(price), 2) if price is not None else None,
    "live_entries_allowed": ok,
    "settlement_reference": (
      (cfg or {}).get("price_feed") or {}
    ).get("settlement_reference", f"CF Benchmarks {index_id}"),
  }


def live_settlement_index_skip_reason(
  tab: dict[str, Any] | None,
  *,
  cfg: dict[str, Any] | None,
  mode: str,
  asset: str = "btc",
) -> str | None:
  """Block live entries when price feed is not the settlement index (BRTI/ERTI)."""
  if str(mode).lower() != "live":
    return None
  lcfg = live_settlement_index_cfg(cfg)
  if not lcfg["enabled"] or not lcfg["require_for_live_entries"]:
    return None
  price, source = settlement_index_quote_from_tab(tab, asset=asset)
  if price is None:
    return SKIP_SETTLEMENT_INDEX_UNAVAILABLE
  if not is_settlement_index_source(source):
    return f"{SKIP_SETTLEMENT_INDEX_NOT_LIVE_PREFIX}{source or 'unknown'}"
  return None
