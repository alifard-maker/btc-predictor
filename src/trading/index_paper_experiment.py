"""SPX/NDX hourly paper trials — ETH hourly live rules (pnl_first) in paper mode."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.assets import INDEX_ASSETS, asset_cfg, asset_enabled
from src.trading.eth_paper_experiment import settings_patch_from_eth_bot_yaml


def index_bot_cfg(cfg: dict[str, Any] | None, asset: str) -> dict[str, Any]:
  return dict(asset_cfg(cfg or {}, asset.lower()).get("hourly", {}).get("bot") or {})


def index_paper_experiment_cfg(cfg: dict[str, Any] | None, asset: str) -> dict[str, Any]:
  return dict(index_bot_cfg(cfg, asset).get("paper_experiment") or {})


def index_paper_experiment_active(cfg: dict[str, Any] | None, asset: str) -> bool:
  return bool(index_paper_experiment_cfg(cfg, asset).get("enabled"))


def _apply_stats_epoch(store: Any, epoch_raw: str | None) -> None:
  if not epoch_raw:
    return
  try:
    epoch = datetime.fromisoformat(str(epoch_raw).replace("Z", "+00:00"))
    with store._connect() as conn:
      from src.trading.bot_runtime import set_stats_epoch_at

      set_stats_epoch_at(conn, epoch.isoformat())
  except (ValueError, TypeError):
    pass


def seed_index_paper_settings_from_cfg(
  store: Any,
  cfg: dict[str, Any] | None,
  asset: str,
  *,
  source: str = "index_paper_experiment",
) -> dict[str, Any]:
  """Apply yaml bot defaults to SPX/NDX hourly store (paper trial arm)."""
  asset = asset.lower()
  exp = index_paper_experiment_cfg(cfg, asset)
  if not exp.get("enabled") or not exp.get("sync_settings_on_arm", True):
    return {"ok": True, "skipped": True, "reason": "experiment_disabled_or_sync_off", "asset": asset}

  bot = index_bot_cfg(cfg, asset)
  if str(bot.get("mode") or "").lower() != "paper":
    return {"ok": False, "skipped": True, "reason": "index_bot_not_paper_in_yaml", "asset": asset}

  patch = settings_patch_from_eth_bot_yaml(bot)
  cur = store.get_settings()
  merged = {**cur.to_dict(), **patch}
  changed = [k for k, v in patch.items() if cur.to_dict().get(k) != v]
  epoch_raw = bot.get("experiment_start_at")

  if not changed and not epoch_raw:
    return {
      "ok": True,
      "synced": True,
      "asset": asset,
      "changed_fields": [],
      "unchanged": True,
      "enabled": cur.enabled,
      "mode": cur.mode,
      "continuous": cur.continuous,
    }

  if changed:
    from src.trading.hourly_bot_store import HourlyBotSettings

    store.save_settings(HourlyBotSettings.from_dict(merged), source=source)
  _apply_stats_epoch(store, str(epoch_raw) if epoch_raw else None)

  return {
    "ok": True,
    "synced": True,
    "asset": asset,
    "changed_fields": changed,
    "enabled": merged.get("enabled"),
    "mode": merged.get("mode"),
    "continuous": merged.get("continuous"),
  }


def ensure_index_paper_experiments(loop: Any) -> dict[str, Any]:
  """Seed SPX/NDX paper trial stores from yaml on startup."""
  results: dict[str, Any] = {}
  for asset in INDEX_ASSETS:
    if not asset_enabled(loop.cfg, asset):
      continue
    if not index_paper_experiment_active(loop.cfg, asset):
      results[asset] = {"ok": True, "skipped": True, "reason": "paper_experiment_disabled"}
      continue
    acfg = getattr(loop, "_index_cfgs", {}).get(asset)
    if acfg is None:
      acfg = asset_cfg(loop.cfg, asset)
    store = loop.hourly_bot_store(asset)
    results[asset] = seed_index_paper_settings_from_cfg(
      store,
      acfg,
      asset,
      source="index_paper_experiment_boot",
    )
  return {
    "ok": True,
    "assets": results,
    "checked_at": datetime.now(timezone.utc).isoformat(),
  }
