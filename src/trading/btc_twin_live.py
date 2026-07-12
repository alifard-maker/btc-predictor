"""BTC hourly twin-live arm — same mid-hour / P&L-first mechanics as ETH live mirror."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.trading.bot_runtime import set_stats_epoch_at
from src.trading.hourly_bot_store import HourlyBotSettings, HourlyBotStore


def btc_bot_cfg(cfg: dict[str, Any] | None) -> dict[str, Any]:
  return dict((((cfg or {}).get("hourly") or {}).get("bot") or {}))


def btc_twin_live_cfg(cfg: dict[str, Any] | None) -> dict[str, Any]:
  return dict(btc_bot_cfg(cfg).get("twin_live") or {})


def btc_twin_live_active(cfg: dict[str, Any] | None) -> bool:
  twin = btc_twin_live_cfg(cfg)
  if twin:
    return bool(twin.get("enabled", False))
  # Fallback: manager flag alone can arm when twin_live block omitted
  mgr = dict((cfg or {}).get("pnl_first_manager") or {})
  return bool(mgr.get("allow_btc_live", False))


def btc_twin_experiment_start_at(cfg: dict[str, Any] | None) -> datetime | None:
  twin = btc_twin_live_cfg(cfg)
  raw = twin.get("experiment_start_at") or btc_bot_cfg(cfg).get("experiment_start_at")
  if not raw:
    return None
  try:
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
  except ValueError:
    return None


def _apply_stats_epoch(store: HourlyBotStore, epoch_raw: str | None) -> None:
  if not epoch_raw:
    return
  try:
    epoch = datetime.fromisoformat(str(epoch_raw).replace("Z", "+00:00"))
    if epoch.tzinfo is None:
      epoch = epoch.replace(tzinfo=timezone.utc)
    with store._connect() as conn:
      set_stats_epoch_at(conn, epoch.isoformat())
  except (ValueError, TypeError):
    pass


def seed_btc_twin_live_from_cfg(
  store: HourlyBotStore,
  cfg: dict[str, Any] | None,
  *,
  source: str = "btc_twin_live",
) -> dict[str, Any]:
  """Arm BTC hourly store for twin live (enabled + live + continuous + twin epoch)."""
  if not btc_twin_live_active(cfg):
    return {"ok": True, "skipped": True, "reason": "btc_twin_live_disabled"}

  bot = btc_bot_cfg(cfg)
  twin = btc_twin_live_cfg(cfg)
  patch = {
    "enabled": True,
    "mode": "live",
    "continuous": bool(twin.get("continuous_enabled", bot.get("continuous_enabled", True))),
    "paper_auto_refill": False,
    "max_spend_per_hour_usd": float(
      twin.get("max_spend_per_hour_usd", bot.get("max_spend_per_hour_usd", 15.0))
    ),
    "allow_strong": bool(bot.get("allow_strong", False)),
    "allow_actionable": bool(bot.get("allow_actionable", False)),
    "use_accumulated_profit": bool(bot.get("use_accumulated_profit", False)),
    "profit_use_pct": float(bot.get("profit_use_pct", 100.0)),
    "take_profit_enabled": bool(bot.get("take_profit_enabled", True)),
    "take_profit_mode": str(bot.get("take_profit_mode") or "hybrid"),
    "take_profit_pct": float(bot.get("take_profit_pct", 0.25)),
    "take_profit_usd": float(bot.get("take_profit_usd", 0.0)),
    "trail_arm_profit_pct": float(bot.get("trail_arm_profit_pct", 0.08)),
    "trail_giveback_pct": float(bot.get("trail_giveback_pct", 0.35)),
    "trail_arm_profit_usd": float(bot.get("trail_arm_profit_usd", 0.50)),
    "min_take_profit_pct": float(bot.get("min_take_profit_pct", 0.10)),
    "max_take_profit_pct": float(bot.get("max_take_profit_pct", 0.40)),
    "min_hold_seconds": int(bot.get("min_hold_seconds", 90)),
    "profit_exit_cooldown_seconds": int(bot.get("profit_exit_cooldown_seconds", 60)),
    "auto_stopped": False,
    "auto_stop_reason": None,
  }
  cur = store.get_settings()
  merged = {**cur.to_dict(), **patch}
  changed = [k for k, v in patch.items() if cur.to_dict().get(k) != v]
  epoch_raw = twin.get("experiment_start_at") or bot.get("experiment_start_at")

  if not changed and not epoch_raw:
    return {
      "ok": True,
      "synced": True,
      "changed_fields": [],
      "unchanged": True,
      "enabled": cur.enabled,
      "mode": cur.mode,
      "continuous": cur.continuous,
    }

  if changed:
    store.save_settings(HourlyBotSettings.from_dict(merged), source=source)
  _apply_stats_epoch(store, str(epoch_raw) if epoch_raw else None)

  return {
    "ok": True,
    "synced": True,
    "changed_fields": changed,
    "enabled": merged.get("enabled"),
    "mode": merged.get("mode"),
    "continuous": merged.get("continuous"),
  }
