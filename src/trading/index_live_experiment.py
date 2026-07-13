"""SPX/NDX hourly live mirror — ETH pnl_first mechanics on Kalshi (parallel to paper trial)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.assets import INDEX_ASSETS, asset_cfg, asset_enabled
from src.trading.index_paper_experiment import index_bot_cfg
from src.trading.eth_paper_experiment import settings_patch_from_eth_bot_yaml


def index_live_mirror_cfg(cfg: dict[str, Any] | None, asset: str) -> dict[str, Any]:
  return dict(index_bot_cfg(cfg, asset).get("live_mirror") or {})


def index_live_runtime_armed(cfg: dict[str, Any] | None, asset: str) -> bool:
  from src.trading.pnl_first_railway_manager import load_manager_state

  state = load_manager_state(cfg)
  return bool((state.get("index_live_armed") or {}).get(asset.lower()))


def set_index_live_runtime_armed(cfg: dict[str, Any] | None, asset: str, armed: bool) -> None:
  from src.trading.pnl_first_railway_manager import load_manager_state, save_manager_state

  state = load_manager_state(cfg)
  armed_map = dict(state.get("index_live_armed") or {})
  armed_map[asset.lower()] = bool(armed)
  state["index_live_armed"] = armed_map
  save_manager_state(state, cfg)


def index_live_mirror_active(cfg: dict[str, Any] | None, asset: str) -> bool:
  mirror = index_live_mirror_cfg(cfg, asset)
  if mirror.get("enabled"):
    return True
  return index_live_runtime_armed(cfg, asset)


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


def seed_index_live_mirror_from_cfg(
  store: Any,
  cfg: dict[str, Any] | None,
  asset: str,
  *,
  source: str = "index_live_mirror",
) -> dict[str, Any]:
  """Apply yaml defaults to SPX/NDX hourly_live store (parallel live arm)."""
  asset = asset.lower()
  if not index_live_mirror_active(cfg, asset):
    return {"ok": True, "skipped": True, "reason": "live_mirror_disabled", "asset": asset}

  bot = index_bot_cfg(cfg, asset)
  mirror = index_live_mirror_cfg(cfg, asset)
  patch = settings_patch_from_eth_bot_yaml(bot)
  patch.update({
    "enabled": True,
    "mode": "live",
    "continuous": bool(mirror.get("continuous_enabled", bot.get("continuous_enabled", True))),
    "paper_auto_refill": False,
    "max_spend_per_hour_usd": float(
      mirror.get("max_spend_per_hour_usd", bot.get("max_spend_per_hour_usd", 15.0))
    ),
    "auto_stopped": False,
    "auto_stop_reason": None,
  })
  cur = store.get_settings()
  merged = {**cur.to_dict(), **patch}
  changed = [k for k, v in patch.items() if cur.to_dict().get(k) != v]
  epoch_raw = mirror.get("experiment_start_at") or bot.get("experiment_start_at")

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

  from src.trading.hourly_bot_store import HourlyBotSettings

  if changed:
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


def disarm_index_live_mirror(
  store: Any,
  cfg: dict[str, Any] | None,
  asset: str,
  *,
  source: str = "index_live_mirror_disarm",
) -> dict[str, Any]:
  asset = asset.lower()
  set_index_live_runtime_armed(cfg, asset, False)
  cur = store.get_settings()
  if not cur.enabled:
    return {"ok": True, "asset": asset, "disarmed": True, "unchanged": True}
  from src.trading.hourly_bot_store import HourlyBotSettings

  merged = {**cur.to_dict(), "enabled": False}
  store.save_settings(HourlyBotSettings.from_dict(merged), source=source)
  return {"ok": True, "asset": asset, "disarmed": True, "enabled": False}


def ensure_index_live_experiments(loop: Any) -> dict[str, Any]:
  """Seed SPX/NDX hourly_live stores when live mirror is armed."""
  results: dict[str, Any] = {}
  for asset in INDEX_ASSETS:
    if not asset_enabled(loop.cfg, asset):
      continue
    if not index_live_mirror_active(loop.cfg, asset):
      results[asset] = {"ok": True, "skipped": True, "reason": "live_mirror_disabled"}
      continue
    results[asset] = sync_index_live_store_if_armed(
      loop,
      loop.cfg,
      asset,
      source="index_live_experiment_boot",
    )
  return {
    "ok": True,
    "assets": results,
    "checked_at": datetime.now(timezone.utc).isoformat(),
  }


def _cached_index_hourly_tab(loop: Any, asset: str) -> dict[str, Any] | None:
  """Fast tab for preflight — reuse bot-status cache, avoid full prediction rebuild."""
  try:
    tab = loop._hourly_tab_for_bot_status(asset)
    if tab and tab.get("ok"):
      return tab
  except Exception:
    pass
  try:
    return loop.index_hourly_prediction(asset, include_bot=False)
  except Exception:
    return None


def sync_index_live_store_if_armed(
  loop: Any,
  cfg: dict[str, Any] | None,
  asset: str,
  *,
  source: str = "index_live_sync",
) -> dict[str, Any]:
  """Keep hourly_live store enabled when runtime live arm is on."""
  asset = asset.lower()
  if not index_live_runtime_armed(cfg, asset):
    return {"ok": True, "skipped": True, "reason": "not_runtime_armed", "asset": asset}
  store = loop.hourly_bot_store(asset, kind="hourly_live")
  settings = store.get_settings()
  needs = (
    not settings.enabled
    or str(settings.mode or "").lower() != "live"
    or not settings.continuous
  )
  if not needs:
    return {"ok": True, "synced": True, "unchanged": True, "asset": asset, "enabled": settings.enabled}
  return seed_index_live_mirror_from_cfg(store, cfg, asset, source=source)


def run_index_live_preflight(
  loop: Any,
  cfg: dict[str, Any] | None,
  asset: str,
  *,
  lite: bool = False,
) -> dict[str, Any]:
  """Preflight checks before arming SPX/NDX live mirror."""
  asset = asset.lower()
  issues: list[str] = []
  warnings: list[str] = []
  detail: dict[str, Any] = {"asset": asset, "lite": lite}

  from src.trading.pnl_first_railway_manager import PnlFirstManagerConfig

  mgr = PnlFirstManagerConfig.from_cfg(cfg)
  detail["allow_index_live"] = mgr.allow_index_live
  if not mgr.allow_index_live:
    issues.append("allow_index_live_off")

  if not asset_enabled(cfg, asset):
    issues.append(f"{asset}_disabled_in_config")

  from src.assets import _eth_aligned_index_hourly_bot_overlay

  overlay = _eth_aligned_index_hourly_bot_overlay()
  detail["overlay_present"] = bool(overlay)
  if not overlay:
    issues.append("eth_aligned_overlay_missing")

  bot = index_bot_cfg(cfg, asset)
  profile = str(bot.get("live_mechanics_profile") or "").lower()
  detail["live_mechanics_profile"] = profile or None
  if profile != "pnl_first":
    issues.append(f"profile_not_pnl_first:{profile or 'missing'}")

  paper_exp = dict(bot.get("paper_experiment") or {})
  detail["paper_experiment_enabled"] = bool(paper_exp.get("enabled"))
  if not paper_exp.get("enabled"):
    issues.append("index_paper_experiment_off")

  runtime_armed = index_live_runtime_armed(cfg, asset)
  detail["runtime_armed"] = runtime_armed
  detail["yaml_live_mirror_enabled"] = bool(index_live_mirror_cfg(cfg, asset).get("enabled"))

  tab: dict[str, Any] | None = None
  hourly_tab_issue = False
  try:
    tab = _cached_index_hourly_tab(loop, asset)
    detail["hourly_tab_ok"] = bool(tab and tab.get("ok"))
    event = (tab.get("event") or {}).get("event_ticker") if tab else None
    detail["event_ticker"] = event
    if not tab or not tab.get("ok") or not event:
      hourly_tab_issue = True
  except Exception as exc:
    hourly_tab_issue = True
    issues.append(f"hourly_tab_error:{type(exc).__name__}")
  if hourly_tab_issue:
    if runtime_armed and lite:
      warnings.append("hourly_tab_unavailable")
    else:
      issues.append("hourly_tab_unavailable")

  kalshi = loop._kalshi_for(asset)
  detail["kalshi_authenticated"] = bool(kalshi and getattr(kalshi, "authenticated", False))
  if not detail["kalshi_authenticated"]:
    issues.append("kalshi_not_authenticated")

  from src.trading.bot_settlement_index_gate import build_settlement_index_status, live_settlement_index_cfg

  si_cfg = live_settlement_index_cfg(asset_cfg(cfg, asset))
  detail["live_settlement_index"] = si_cfg
  if si_cfg.get("enabled") and si_cfg.get("require_for_live_entries"):
    try:
      si = build_settlement_index_status(tab, cfg=asset_cfg(cfg, asset))
      if not si.get("ok"):
        quote = loop.live_price_quote(fresh=False, asset=asset)
        if quote and quote.price is not None:
          si = build_settlement_index_status(
            None,
            cfg=asset_cfg(cfg, asset),
            price=quote.price,
            source=quote.source,
          )
      detail["settlement_index"] = si
      if not si.get("ok"):
        if runtime_armed and lite:
          warnings.append("settlement_index_not_live")
        else:
          issues.append("settlement_index_not_live")
    except Exception as exc:
      issues.append(f"settlement_index_error:{type(exc).__name__}")

  from src.trading.us_market_hours import index_trading_allowed

  acfg = asset_cfg(cfg, asset)
  detail["market_hours_open"] = index_trading_allowed(acfg)
  if not detail["market_hours_open"]:
    warnings.append("outside_market_hours")

  live_enabled = False
  try:
    paper_store = loop.hourly_bot_store(asset, kind="hourly")
    paper_settings = paper_store.get_settings()
    detail["paper_settings"] = {
      "enabled": paper_settings.enabled,
      "mode": paper_settings.mode,
      "continuous": paper_settings.continuous,
    }
    if not paper_settings.enabled:
      issues.append("index_paper_disabled")
    if str(paper_settings.mode).lower() != "paper":
      issues.append(f"index_paper_wrong_mode:{paper_settings.mode}")
    if not paper_settings.continuous:
      issues.append("index_paper_continuous_off")
  except Exception as exc:
    issues.append(f"paper_store_error:{type(exc).__name__}")

  try:
    if runtime_armed:
      sync_index_live_store_if_armed(loop, cfg, asset, source="index_live_preflight_sync")
    live_store = loop.hourly_bot_store(asset, kind="hourly_live")
    live_settings = live_store.get_settings()
    live_enabled = bool(live_settings.enabled)
    detail["live_settings"] = {
      "enabled": live_settings.enabled,
      "mode": live_settings.mode,
      "continuous": live_settings.continuous,
    }
    if runtime_armed and not live_settings.enabled:
      warnings.append("live_mirror_auto_bet_off")
    if runtime_armed and not live_settings.continuous:
      warnings.append("live_mirror_continuous_off")
    if not lite:
      open_pos = (
        live_store.all_open_live_positions()
        if live_settings.mode == "live" and hasattr(live_store, "all_open_live_positions")
        else []
      )
      detail["live_open_legs"] = len(open_pos or [])
      detail["live_open_exposure_usd"] = sum(float(p.get("cost_usd") or 0) for p in (open_pos or []))
      if detail["live_open_legs"] or float(detail["live_open_exposure_usd"] or 0) > 0.01:
        issues.append("live_mirror_has_open_legs")
  except Exception as exc:
    issues.append(f"live_store_error:{type(exc).__name__}")

  if not lite:
    try:
      recon = loop.hourly_live_reconcile(asset, kind="hourly_live")
      detail["reconcile"] = {
        "event_ticker": recon.get("event_ticker"),
        "kalshi_only": len(recon.get("kalshi_only") or []),
        "bot_only": len(recon.get("bot_only") or []),
      }
      if detail["reconcile"]["bot_only"]:
        issues.append("reconcile_bot_only")
      if detail["reconcile"]["kalshi_only"]:
        issues.append(f"reconcile_kalshi_only:{detail['reconcile']['kalshi_only']}")
    except Exception as exc:
      issues.append(f"reconcile_error:{type(exc).__name__}")

  return {
    "ok": not issues,
    "issues": issues,
    "warnings": warnings,
    "detail": detail,
    "armed": index_live_mirror_active(cfg, asset),
    "live_enabled": live_enabled,
    "ts": datetime.now(timezone.utc).isoformat(),
  }


def arm_index_live_mirror(loop: Any, cfg: dict[str, Any] | None, asset: str) -> dict[str, Any]:
  """Arm live mirror after preflight passes (runtime toggle — yaml stays disabled)."""
  asset = asset.lower()
  preflight = run_index_live_preflight(loop, cfg, asset, lite=False)
  if not preflight.get("ok"):
    return {
      "ok": False,
      "error": "preflight_failed",
      "preflight": preflight,
    }
  set_index_live_runtime_armed(cfg, asset, True)
  store = loop.hourly_bot_store(asset, kind="hourly_live")
  seed = sync_index_live_store_if_armed(loop, cfg, asset, source="index_live_arm")
  return {
    "ok": True,
    "armed": True,
    "asset": asset,
    "preflight": preflight,
    "seed": seed,
  }
