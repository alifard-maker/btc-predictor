"""Snapshot bot settings at trade entry for honest performance reporting."""

from __future__ import annotations

from typing import Any, Protocol


class HourlySettingsLike(Protocol):
  enabled: bool
  mode: str
  max_spend_per_hour_usd: float
  allow_strong: bool
  allow_actionable: bool
  use_accumulated_profit: bool
  profit_use_pct: float


class Slot15SettingsLike(Protocol):
  enabled: bool
  mode: str
  max_spend_per_slot_usd: float
  allow_strong: bool
  allow_actionable: bool
  use_accumulated_profit: bool
  profit_use_pct: float


def _core_snapshot(
  settings: Any,
  *,
  max_spend: float,
) -> dict[str, Any]:
  return {
    "enabled": bool(settings.enabled),
    "allow_strong": bool(settings.allow_strong),
    "allow_actionable": bool(settings.allow_actionable),
    "use_accumulated_profit": bool(settings.use_accumulated_profit),
    "profit_use_pct": float(getattr(settings, "profit_use_pct", 100.0)),
    "max_spend": float(max_spend),
    "mode": str(settings.mode),
    "free_mode": not settings.allow_strong and not settings.allow_actionable,
  }


def hourly_entry_settings_snapshot(settings: HourlySettingsLike) -> dict[str, Any]:
  return _core_snapshot(settings, max_spend=settings.max_spend_per_hour_usd)


def slot15_entry_settings_snapshot(settings: Slot15SettingsLike) -> dict[str, Any]:
  return _core_snapshot(settings, max_spend=settings.max_spend_per_slot_usd)


def infer_store_meta(db_path: Any) -> tuple[str, str]:
  """Return (asset, bot_type) from a bot store db filename."""
  name = str(db_path).lower()
  bot_type = "slot15" if "slot15" in name else "hourly"
  asset = "eth" if "_eth" in name else "btc"
  return asset, bot_type
