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


def hourly_entry_settings_snapshot(
  settings: HourlySettingsLike,
  *,
  adaptive: dict[str, Any] | None = None,
  hour_momentum: dict[str, Any] | None = None,
  hours_to_settle: float | None = None,
) -> dict[str, Any]:
  snap = _core_snapshot(settings, max_spend=settings.max_spend_per_hour_usd)
  if adaptive:
    snap["adaptive"] = adaptive
  if hour_momentum:
    snap["hour_momentum"] = hour_momentum
  if hours_to_settle is not None:
    try:
      snap["hours_to_settle"] = round(float(hours_to_settle), 4)
      snap["minutes_to_settle"] = round(float(hours_to_settle) * 60.0, 1)
    except (TypeError, ValueError):
      pass
  return snap


def slot15_entry_settings_snapshot(settings: Slot15SettingsLike) -> dict[str, Any]:
  return _core_snapshot(settings, max_spend=settings.max_spend_per_slot_usd)


def infer_store_meta(db_path: Any) -> tuple[str, str]:
  """Return (asset, bot_type) from a bot store db filename."""
  name = str(db_path).lower()
  if "hourly_trial_rally" in name:
    bot_type = "hourly_trial_rally"
  elif "hourly_trial_soft" in name:
    bot_type = "hourly_trial_soft"
  elif "hourly_trial_mech" in name:
    bot_type = "hourly_trial_mech"
  elif "hourly_trial" in name:
    bot_type = "hourly_trial"
  elif "slot15" in name:
    bot_type = "slot15"
  else:
    bot_type = "hourly"
  asset = "eth" if "_eth" in name else "btc"
  return asset, bot_type
