"""$4k/week plan — lane P&L and startup ensure helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.trading.bot_runtime import parse_stats_epoch_at, set_stats_epoch_at, stats_epoch_at
from src.trading.probe_24h import effective_compare_stats_epoch_at, probe_24h_cfg, probe_stats_epoch_iso
from src.trading.terminal_shadow_logger import summarize_track_b_shadow, track_b_epoch_iso


def four_k_week_plan_cfg(cfg: dict[str, Any] | None) -> dict[str, Any]:
  from src.trading.pnl_first_gates import _pnl_first_cfg

  return dict(_pnl_first_cfg(cfg).get("four_k_week_plan") or {})


def four_k_week_plan_active(cfg: dict[str, Any] | None) -> bool:
  return bool(four_k_week_plan_cfg(cfg).get("enabled", True))


def plan_started_at_iso(cfg: dict[str, Any] | None) -> str | None:
  block = four_k_week_plan_cfg(cfg)
  raw = block.get("started_at")
  return str(raw) if raw else None


def eth_slot15_bot_cfg(cfg: dict[str, Any] | None) -> dict[str, Any]:
  return dict((((cfg or {}).get("eth") or {}).get("intra_slot") or {}).get("bot") or {})


def eth_slot15_experiment_start_at(cfg: dict[str, Any] | None) -> datetime | None:
  raw = eth_slot15_bot_cfg(cfg).get("experiment_start_at") or plan_started_at_iso(cfg)
  if not raw:
    return None
  return parse_stats_epoch_at(str(raw))


def _parse_ts(raw: str | None) -> datetime | None:
  if not raw:
    return None
  try:
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
  except ValueError:
    return None


def _lane_pnl_from_store(
  store: Any,
  *,
  mode: str,
  since: datetime | None,
) -> dict[str, Any]:
  trades = store.list_trades(limit=5000)
  if since is not None:
    trades = [t for t in trades if (_parse_ts(t.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc)) >= since]
  trades = [t for t in trades if str(t.get("mode") or "").lower() == mode.lower()]
  enters = [t for t in trades if t.get("action") == "enter" and str(t.get("status") or "") in ("filled", "reconciled")]
  exits = [t for t in trades if t.get("action") == "exit" and str(t.get("status") or "") in ("filled", "reconciled")]
  from src.trading.bot_exit_pnl import effective_exit_pnl_usd

  pnl = round(sum(float(effective_exit_pnl_usd(t) or 0) for t in exits), 2)
  events = {str(t.get("event_ticker") or "") for t in enters if t.get("event_ticker")}
  return {
    "net_pnl_usd": pnl,
    "filled_enters": len(enters),
    "exits": len(exits),
    "periods_with_entries": len({e for e in events if e}),
  }


def _track_a_lane(loop: Any, cfg: dict[str, Any] | None) -> dict[str, Any]:
  from src.trading.compare_paper_twins import compare_store_kinds
  from src.trading.hourly_live_trial_compare import build_hourly_live_trial_compare
  from src.trading.hourly_live_trial_align import HourlyLiveTrialAlignConfig
  from src.assets import asset_cfg

  asset = "eth"
  live_kind, trial_kind = compare_store_kinds(asset)
  live_store = loop.hourly_bot_store(asset, kind=live_kind)
  trial_store = loop.hourly_bot_store(asset, kind=trial_kind)
  acfg = asset_cfg(cfg, asset)
  align = HourlyLiveTrialAlignConfig.from_cfg(acfg, kind="hourly")
  epoch = effective_compare_stats_epoch_at(live_store, cfg)
  compare = build_hourly_live_trial_compare(
    live_store,
    trial_store,
    asset=asset,
    limit_hours=168,
    live_kind=live_kind,
    trial_kind=trial_kind,
    pair_window_seconds=align.compare_pair_window_seconds,
    stats_epoch_at=epoch,
  )
  hours = compare.get("hours") or []
  live_pnl = round(sum(float((h.get("live") or {}).get("net_pnl_usd") or 0) for h in hours), 2)
  trial_pnl = round(sum(float((h.get("trial") or {}).get("net_pnl_usd") or 0) for h in hours), 2)
  matched_hours = sum(1 for h in hours if h.get("both_active"))
  hours_with_live = sum(1 for h in hours if (h.get("live") or {}).get("has_activity"))
  probe = probe_24h_cfg(cfg)
  return {
    "lane": "track_a",
    "label": "Track A · ETH hourly mid-hour (probe)",
    "stats_epoch_at": epoch,
    "probe_24h": {
      "enabled": bool(probe.get("enabled")),
      "max_filled_enters_per_hour": probe.get("max_filled_enters_per_hour"),
      "min_ask_edge_cents": probe.get("min_ask_edge_cents"),
      "max_stake_per_entry_usd": probe.get("max_stake_per_entry_usd"),
    },
    "live": {
      "kind": live_kind,
      "net_pnl_usd": live_pnl,
      "matched_hours": matched_hours,
      "hours_with_live": hours_with_live,
    },
    "trial": {
      "kind": trial_kind,
      "net_pnl_usd": trial_pnl,
    },
    "combined_live_trial_delta_usd": round(live_pnl - trial_pnl, 2),
  }


def _slot15_eth_lane(loop: Any, cfg: dict[str, Any] | None) -> dict[str, Any]:
  since = eth_slot15_experiment_start_at(cfg)
  store = loop.slot15_bot_store("eth")
  settings = store.get_settings()
  stats = _lane_pnl_from_store(store, mode=settings.mode or "paper", since=since)
  return {
    "lane": "slot15_eth",
    "label": "Slot15 · ETH KXETH15M (paper experiment)",
    "stats_epoch_at": since.isoformat() if since else None,
    "enabled": bool(settings.enabled),
    "mode": settings.mode,
    "continuous": bool(settings.continuous),
    **stats,
    "note": "Separate product from Track B terminal shadow — not merged into hourly probe stats.",
  }


def build_four_k_week_plan_report(loop: Any, cfg: dict[str, Any] | None) -> dict[str, Any]:
  plan = four_k_week_plan_cfg(cfg)
  week = int(plan.get("week", 1))
  track_b = summarize_track_b_shadow(cfg, asset="eth")
  lanes = {
    "track_a": _track_a_lane(loop, cfg),
    "track_b_shadow": track_b,
    "slot15_eth": _slot15_eth_lane(loop, cfg),
  }
  week_targets = {
    1: {
      "hour_cap_usd": 15,
      "stake_usd": 2.5,
      "max_enters_per_hour": 2,
      "min_edge_cents": 18,
      "capital_usd": "200–500",
      "target_weekly_usd": "25–75",
    },
    2: {
      "hour_cap_usd": 30,
      "stake_usd": 5,
      "max_enters_per_hour": 2,
      "min_edge_cents": 18,
      "capital_usd": "1k–2k",
      "target_weekly_usd": "50–150",
    },
  }
  gates_week_1 = {
    "matched_hours_min": 14,
    "eth_live_pnl_per_hour_min_usd": 0.0,
    "max_filled_enters_per_hour": 2,
    "leg_stop_loss_share_max": 0.5,
  }
  return {
    "ok": True,
    "plan": "4k_week",
    "name": "$4k/week plan",
    "week": week,
    "started_at": plan_started_at_iso(cfg),
    "week_config": week_targets.get(week, week_targets[1]),
    "gates": gates_week_1 if week == 1 else None,
    "lanes": lanes,
    "track_b_epoch_at": track_b_epoch_iso(cfg),
    "probe_stats_epoch_at": probe_stats_epoch_iso(cfg),
  }


def _apply_stats_epoch(store: Any, epoch_raw: str | None) -> dict[str, Any]:
  if not epoch_raw:
    return {"updated": False, "reason": "no_epoch"}
  epoch = parse_stats_epoch_at(str(epoch_raw))
  if epoch is None:
    return {"updated": False, "error": f"invalid_epoch:{epoch_raw}"}
  with store._connect() as conn:
    cur_iso = stats_epoch_at(conn)
    cur_dt = parse_stats_epoch_at(cur_iso)
    if cur_dt is None or epoch > cur_dt:
      set_stats_epoch_at(conn, epoch.isoformat())
      return {"stats_epoch_at": epoch.isoformat(), "updated": True}
    return {"stats_epoch_at": cur_iso, "updated": False}


def ensure_eth_slot15_paper_plan(loop: Any, cfg: dict[str, Any] | None) -> dict[str, Any]:
  """Arm ETH slot15 paper bot + stats epoch for $4k/week plan."""
  from src.trading.slot15_bot_store import Slot15BotSettings
  from src.trading.pnl_first_railway_manager import PnlFirstManagerConfig

  mgr = PnlFirstManagerConfig.from_cfg(cfg)
  bot_cfg = eth_slot15_bot_cfg(cfg)
  if not mgr.allow_eth_slot15_paper:
    return {"ok": True, "skipped": True, "reason": "allow_eth_slot15_paper_false"}

  store = loop.slot15_bot_store("eth")
  settings = store.get_settings()
  merged = dict(settings.to_dict())
  changed = False
  if bool(bot_cfg.get("enabled", True)) and not settings.enabled:
    merged["enabled"] = True
    changed = True
  if bool(bot_cfg.get("continuous_enabled", True)) and not settings.continuous:
    merged["continuous"] = True
    changed = True
  want_mode = str(bot_cfg.get("mode") or "paper").lower()
  if want_mode and str(settings.mode or "").lower() != want_mode:
    merged["mode"] = want_mode
    changed = True
  if changed:
    store.save_settings(Slot15BotSettings.from_dict(merged), source="4k_week_plan")

  epoch_raw = bot_cfg.get("experiment_start_at") or plan_started_at_iso(cfg)
  epoch_result = _apply_stats_epoch(store, str(epoch_raw) if epoch_raw else None)
  return {
    "ok": True,
    "armed": changed or bool(settings.enabled),
    "settings": store.get_settings().to_dict(),
    "stats_epoch": epoch_result,
  }


def ensure_four_k_week_plan(loop: Any, cfg: dict[str, Any] | None) -> dict[str, Any]:
  from src.trading.compare_paper_twins import ensure_compare_paper_twins
  from src.trading.probe_24h import ensure_probe_stats_epoch

  twins = ensure_compare_paper_twins(loop, cfg)
  probe = ensure_probe_stats_epoch(loop, cfg)
  slot15 = ensure_eth_slot15_paper_plan(loop, cfg)
  return {
    "ok": True,
    "compare_paper_twins": twins,
    "probe_stats_epoch": probe,
    "eth_slot15_paper": slot15,
    "track_b_shadow_enabled": bool(
      ((cfg or {}).get("pnl_first") or {}).get("track_b_shadow", {}).get("enabled")
    ),
  }
