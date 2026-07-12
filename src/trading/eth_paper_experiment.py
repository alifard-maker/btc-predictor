"""ETH hourly paper harness — config sync and health for mid-hour experiment."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.trading.hourly_bot_store import HourlyBotSettings, HourlyBotStore


def eth_bot_cfg(cfg: dict[str, Any] | None) -> dict[str, Any]:
  return dict((((cfg or {}).get("eth") or {}).get("hourly") or {}).get("bot") or {})


def eth_experiment_start_at(cfg: dict[str, Any] | None) -> datetime | None:
  """ETH paper experiment epoch from yaml (not bot store stats_epoch_at)."""
  raw = eth_bot_cfg(cfg).get("experiment_start_at")
  if not raw:
    return None
  try:
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
  except ValueError:
    return None


def eth_paper_experiment_cfg(cfg: dict[str, Any] | None) -> dict[str, Any]:
  return dict(eth_bot_cfg(cfg).get("paper_experiment") or {})


def eth_paper_experiment_active(cfg: dict[str, Any] | None) -> bool:
  return bool(eth_paper_experiment_cfg(cfg).get("enabled"))


def eth_live_mirror_cfg(cfg: dict[str, Any] | None) -> dict[str, Any]:
  return dict(eth_bot_cfg(cfg).get("live_mirror") or {})


def eth_live_mirror_active(cfg: dict[str, Any] | None) -> bool:
  return bool(eth_live_mirror_cfg(cfg).get("enabled"))


def _apply_stats_epoch(store: HourlyBotStore, epoch_raw: str | None) -> None:
  if not epoch_raw:
    return
  try:
    epoch = datetime.fromisoformat(str(epoch_raw).replace("Z", "+00:00"))
    with store._connect() as conn:
      from src.trading.bot_runtime import set_stats_epoch_at

      set_stats_epoch_at(conn, epoch.isoformat())
  except (ValueError, TypeError):
    pass


def seed_eth_live_mirror_from_cfg(
  store: HourlyBotStore,
  cfg: dict[str, Any] | None,
  *,
  source: str = "eth_live_mirror",
) -> dict[str, Any]:
  """Apply yaml defaults to ETH hourly_live mirror store (parallel live arm)."""
  mirror = eth_live_mirror_cfg(cfg)
  if not mirror.get("enabled"):
    return {"ok": True, "skipped": True, "reason": "live_mirror_disabled"}

  eth_bot = eth_bot_cfg(cfg)
  patch = settings_patch_from_eth_bot_yaml(eth_bot)
  patch.update({
    "enabled": True,
    "mode": "live",
    "continuous": bool(mirror.get("continuous_enabled", eth_bot.get("continuous_enabled", True))),
    "paper_auto_refill": False,
    "max_spend_per_hour_usd": float(
      mirror.get("max_spend_per_hour_usd", eth_bot.get("max_spend_per_hour_usd", 15.0))
    ),
  })
  cur = store.get_settings()
  merged = {**cur.to_dict(), **patch}
  changed = [k for k, v in patch.items() if cur.to_dict().get(k) != v]
  epoch_raw = mirror.get("experiment_start_at") or eth_bot.get("experiment_start_at")

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


def settings_patch_from_eth_bot_yaml(eth_bot: dict[str, Any]) -> dict[str, Any]:
  """Map eth.hourly.bot yaml → HourlyBotSettings fields (experiment sync)."""
  patch: dict[str, Any] = {
    "enabled": bool(eth_bot.get("enabled", True)),
    "mode": str(eth_bot.get("mode") or "paper"),
    "continuous": bool(eth_bot.get("continuous_enabled", True)),
    "max_spend_per_hour_usd": float(eth_bot.get("max_spend_per_hour_usd", 15.0)),
    "paper_auto_refill": bool(eth_bot.get("paper_auto_refill", True)),
    "use_accumulated_profit": bool(eth_bot.get("use_accumulated_profit", False)),
    "profit_use_pct": float(eth_bot.get("profit_use_pct", 100.0)),
    "take_profit_enabled": bool(eth_bot.get("take_profit_enabled", True)),
    "take_profit_mode": str(eth_bot.get("take_profit_mode") or "hybrid"),
    "take_profit_pct": float(eth_bot.get("take_profit_pct", 0.25)),
    "take_profit_usd": float(eth_bot.get("take_profit_usd", 0.0)),
    "trail_arm_profit_pct": float(eth_bot.get("trail_arm_profit_pct", 0.08)),
    "trail_giveback_pct": float(eth_bot.get("trail_giveback_pct", 0.35)),
    "trail_arm_profit_usd": float(eth_bot.get("trail_arm_profit_usd", 0.50)),
    "min_take_profit_pct": float(eth_bot.get("min_take_profit_pct", 0.10)),
    "max_take_profit_pct": float(eth_bot.get("max_take_profit_pct", 0.40)),
    "min_hold_seconds": int(eth_bot.get("min_hold_seconds", 90)),
    "profit_exit_cooldown_seconds": int(eth_bot.get("profit_exit_cooldown_seconds", 60)),
    "auto_stopped": False,
    "auto_stop_reason": None,
  }
  return patch


def seed_eth_paper_settings_from_cfg(
  store: HourlyBotStore,
  cfg: dict[str, Any] | None,
  *,
  source: str = "eth_paper_experiment",
) -> dict[str, Any]:
  """Apply yaml bot defaults to ETH hourly store (manager arm / deploy sync)."""
  exp = eth_paper_experiment_cfg(cfg)
  if not exp.get("enabled") or not exp.get("sync_settings_on_arm", True):
    return {"ok": True, "skipped": True, "reason": "experiment_disabled_or_sync_off"}

  eth_bot = eth_bot_cfg(cfg)
  if str(eth_bot.get("mode") or "").lower() != "paper":
    return {"ok": False, "skipped": True, "reason": "eth_bot_not_paper_in_yaml"}

  patch = settings_patch_from_eth_bot_yaml(eth_bot)
  cur = store.get_settings()
  merged = {**cur.to_dict(), **patch}
  changed = [k for k, v in patch.items() if cur.to_dict().get(k) != v]
  epoch_raw = eth_bot.get("experiment_start_at")

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


def check_eth_paper_harness(loop: Any, cfg: dict[str, Any] | None) -> dict[str, Any]:
  """Health snapshot for ETH paper + optional live mirror arms."""
  paper_active = eth_paper_experiment_active(cfg)
  live_active = eth_live_mirror_active(cfg)
  if not paper_active and not live_active:
    return {"ok": True, "skipped": True}

  issues: list[str] = []
  arms: dict[str, Any] = {}
  try:
    eth_bot = eth_bot_cfg(cfg)
    mid_hour = bool(
      ((cfg or {}).get("pnl_first") or {}).get("mid_hour_entry", {}).get("eth_enabled")
      or ((cfg or {}).get("pnl_first") or {}).get("mid_hour_entry", {}).get("eth_paper_enabled")
    )
    guards = {
      "soft_rally_enabled": bool((eth_bot.get("soft_rally") or {}).get("enabled")),
      "whipsaw_regime_block": bool(
        (eth_bot.get("whipsaw_guard") or {}).get("block_entries_when_regime_blocked")
      ),
      "mid_hour_eth": mid_hour,
      "s1_only": bool(
        ((eth_bot.get("live_inventory") or {}).get("max_same_side_range_legs", 1) == 0)
      ),
    }

    if paper_active:
      store = loop.hourly_bot_store("eth", kind="hourly")
      settings = store.get_settings()
      skip = store.last_skip_reason()
      paper_issues: list[str] = []
      if not settings.enabled:
        paper_issues.append("eth_paper_disabled")
      if not settings.continuous:
        paper_issues.append("eth_paper_continuous_off")
      if str(settings.mode).lower() != "paper":
        paper_issues.append(f"eth_paper_wrong_mode:{settings.mode}")
      if skip in ("auto_bet_off", "continuous_mode_off"):
        paper_issues.append(f"eth_fatal_skip:{skip}")
      issues.extend(paper_issues)
      arms["paper"] = {
        "ok": not paper_issues,
        "issues": paper_issues,
        "settings": {
          "enabled": settings.enabled,
          "mode": settings.mode,
          "continuous": settings.continuous,
          "max_spend_per_hour_usd": settings.max_spend_per_hour_usd,
        },
        "last_skip_reason": skip,
        "recent_exit_count": sum(
          1
          for t in store.list_trades(limit=500)
          if str(t.get("action") or "") == "exit"
          and str(t.get("status") or "") in ("filled", "reconciled")
        ),
      }

    if live_active:
      live_store = loop.hourly_bot_store("eth", kind="hourly_live")
      live_settings = live_store.get_settings()
      live_skip = live_store.last_skip_reason()
      live_issues: list[str] = []
      if not live_settings.enabled:
        live_issues.append("eth_live_disabled")
      if not live_settings.continuous:
        live_issues.append("eth_live_continuous_off")
      if str(live_settings.mode).lower() != "live":
        live_issues.append(f"eth_live_wrong_mode:{live_settings.mode}")
      if live_skip in ("auto_bet_off", "continuous_mode_off"):
        live_issues.append(f"eth_live_fatal_skip:{live_skip}")
      issues.extend(live_issues)
      arms["live_mirror"] = {
        "ok": not live_issues,
        "issues": live_issues,
        "settings": {
          "enabled": live_settings.enabled,
          "mode": live_settings.mode,
          "continuous": live_settings.continuous,
          "max_spend_per_hour_usd": live_settings.max_spend_per_hour_usd,
        },
        "last_skip_reason": live_skip,
        "recent_exit_count": sum(
          1
          for t in live_store.list_trades(limit=500)
          if str(t.get("action") or "") == "exit"
          and str(t.get("status") or "") in ("filled", "reconciled")
        ),
      }

    return {
      "ok": not issues,
      "issues": issues,
      "arms": arms,
      "guards": guards,
      "experiment": eth_paper_experiment_cfg(cfg),
      "live_mirror": eth_live_mirror_cfg(cfg),
      "checked_at": datetime.now(timezone.utc).isoformat(),
    }
  except Exception as exc:
    return {"ok": False, "issues": [f"eth_paper_check_error:{type(exc).__name__}:{exc}"]}
