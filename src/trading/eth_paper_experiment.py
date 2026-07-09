"""ETH hourly paper harness — config sync and health for mid-hour experiment."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.trading.hourly_bot_store import HourlyBotSettings, HourlyBotStore


def eth_bot_cfg(cfg: dict[str, Any] | None) -> dict[str, Any]:
  return dict((((cfg or {}).get("eth") or {}).get("hourly") or {}).get("bot") or {})


def eth_paper_experiment_cfg(cfg: dict[str, Any] | None) -> dict[str, Any]:
  return dict(eth_bot_cfg(cfg).get("paper_experiment") or {})


def eth_paper_experiment_active(cfg: dict[str, Any] | None) -> bool:
  return bool(eth_paper_experiment_cfg(cfg).get("enabled"))


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

  store.save_settings(HourlyBotSettings.from_dict(merged), source=source)

  epoch_raw = eth_bot.get("experiment_start_at")
  if epoch_raw:
    try:
      epoch = datetime.fromisoformat(str(epoch_raw).replace("Z", "+00:00"))
      with store._connect() as conn:
        from src.trading.bot_runtime import set_stats_epoch_at

        set_stats_epoch_at(conn, epoch.isoformat())
    except (ValueError, TypeError):
      pass

  return {
    "ok": True,
    "synced": True,
    "changed_fields": changed,
    "enabled": merged.get("enabled"),
    "mode": merged.get("mode"),
    "continuous": merged.get("continuous"),
  }


def check_eth_paper_harness(loop: Any, cfg: dict[str, Any] | None) -> dict[str, Any]:
  """Health snapshot for ETH paper mid-hour experiment."""
  if not eth_paper_experiment_active(cfg):
    return {"ok": True, "skipped": True}

  issues: list[str] = []
  try:
    store = loop.hourly_bot_store("eth", kind="hourly")
    settings = store.get_settings()
    skip = store.last_skip_reason()

    if not settings.enabled:
      issues.append("eth_paper_disabled")
    if not settings.continuous:
      issues.append("eth_paper_continuous_off")
    if str(settings.mode).lower() != "paper":
      issues.append(f"eth_paper_wrong_mode:{settings.mode}")
    if skip in ("auto_bet_off", "continuous_mode_off"):
      issues.append(f"eth_fatal_skip:{skip}")

    exp_cfg = eth_paper_experiment_cfg(cfg)
    eth_bot = eth_bot_cfg(cfg)
    return {
      "ok": not issues,
      "issues": issues,
      "settings": {
        "enabled": settings.enabled,
        "mode": settings.mode,
        "continuous": settings.continuous,
        "max_spend_per_hour_usd": settings.max_spend_per_hour_usd,
        "paper_auto_refill": settings.paper_auto_refill,
        "profit_use_pct": settings.profit_use_pct,
      },
      "last_skip_reason": skip,
      "recent_exit_count": sum(
        1
        for t in store.list_trades(limit=500)
        if str(t.get("action") or "") == "exit"
        and str(t.get("status") or "") in ("filled", "reconciled")
      ),
      "guards": {
        "soft_rally_enabled": bool((eth_bot.get("soft_rally") or {}).get("enabled")),
        "whipsaw_regime_block": bool(
          (eth_bot.get("whipsaw_guard") or {}).get("block_entries_when_regime_blocked")
        ),
        "mid_hour_eth_paper": bool(
          ((cfg or {}).get("pnl_first") or {}).get("mid_hour_entry", {}).get("eth_paper_enabled")
        ),
      },
      "experiment": exp_cfg,
      "checked_at": datetime.now(timezone.utc).isoformat(),
    }
  except Exception as exc:
    return {"ok": False, "issues": [f"eth_paper_check_error:{type(exc).__name__}:{exc}"]}
