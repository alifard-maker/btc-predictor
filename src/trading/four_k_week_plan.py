"""$4k/week plan — lane P&L and startup ensure helpers."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

from src.trading.bot_runtime import parse_stats_epoch_at, set_stats_epoch_at, stats_epoch_at
from src.trading.probe_24h import effective_compare_stats_epoch_at, probe_24h_cfg, probe_stats_epoch_iso
from src.trading.terminal_shadow_logger import (
  _epoch_ok,
  shadow_log_dir,
  summarize_track_b_shadow,
  track_b_epoch_iso,
  track_b_shadow_active,
)

log = logging.getLogger(__name__)

_REPORT_CACHE: dict[str, Any] = {"mono_at": 0.0, "payload": None}
_REPORT_CACHE_TTL_SEC = 30.0
_REVISION_CACHE: dict[str, Any] = {"mono_at": 0.0, "payload": None}
_REVISION_CACHE_TTL_SEC = 20.0


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
  trade_limit: int = 800,
) -> dict[str, Any]:
  """Lightweight closed-trade summary since epoch (plan card only)."""
  trades = store.list_trades(limit=trade_limit)
  if since is not None:
    trades = [t for t in trades if (_parse_ts(t.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc)) >= since]
  trades = [t for t in trades if str(t.get("mode") or "").lower() == mode.lower()]
  enters = [t for t in trades if t.get("action") == "enter" and str(t.get("status") or "") in ("filled", "reconciled")]
  exits = [t for t in trades if t.get("action") == "exit" and str(t.get("status") or "") in ("filled", "reconciled")]
  from src.trading.bot_exit_pnl import effective_exit_pnl_usd

  pnl = round(sum(float(effective_exit_pnl_usd(t) or 0) for t in exits), 2)
  events = {str(t.get("event_ticker") or "") for t in enters if t.get("event_ticker")}
  trial_events = {str(t.get("event_ticker") or "") for t in enters if t.get("event_ticker")}
  return {
    "net_pnl_usd": pnl,
    "filled_enters": len(enters),
    "exits": len(exits),
    "periods_with_entries": len({e for e in events if e}),
    "_entry_events": trial_events,
  }


def _matched_hours_since_epoch(live_store: Any, trial_store: Any, *, since: datetime | None) -> int:
  """Count hours where both live and trial had filled enters (cheap set intersect)."""
  live = _lane_pnl_from_store(live_store, mode="live", since=since, trade_limit=400)
  trial_mode = str(trial_store.get_settings().mode or "paper").lower()
  trial = _lane_pnl_from_store(trial_store, mode=trial_mode, since=since, trade_limit=400)
  live_ev = set(live.pop("_entry_events", set()))
  trial_ev = set(trial.pop("_entry_events", set()))
  return len(live_ev & trial_ev)


def _track_a_lane(loop: Any, cfg: dict[str, Any] | None) -> dict[str, Any]:
  from src.trading.compare_paper_twins import compare_store_kinds

  asset = "eth"
  live_kind, trial_kind = compare_store_kinds(asset)
  live_store = loop.hourly_bot_store(asset, kind=live_kind)
  trial_store = loop.hourly_bot_store(asset, kind=trial_kind)
  epoch = effective_compare_stats_epoch_at(live_store, cfg)
  epoch_dt = parse_stats_epoch_at(epoch)
  live_stats = _lane_pnl_from_store(live_store, mode="live", since=epoch_dt, trade_limit=500)
  live_stats.pop("_entry_events", None)
  trial_mode = str(trial_store.get_settings().mode or "paper").lower()
  trial_stats = _lane_pnl_from_store(trial_store, mode=trial_mode, since=epoch_dt, trade_limit=500)
  trial_stats.pop("_entry_events", None)
  matched_hours = _matched_hours_since_epoch(live_store, trial_store, since=epoch_dt)
  probe = probe_24h_cfg(cfg)
  live_pnl = float(live_stats.get("net_pnl_usd") or 0)
  trial_pnl = float(trial_stats.get("net_pnl_usd") or 0)
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
      "hours_with_live": live_stats.get("periods_with_entries", 0),
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
  from src.trading.kalshi_portfolio_pnl import kalshi_wallet_snapshot, kalshi_portfolio_pnl_store

  plan = four_k_week_plan_cfg(cfg)
  week = int(plan.get("week", 1))
  kalshi = getattr(loop, "kalshi", None)
  kalshi_wallet = kalshi_wallet_snapshot(kalshi, cfg, store=kalshi_portfolio_pnl_store(cfg))
  try:
    track_b = summarize_track_b_shadow(cfg, asset="eth")
    lanes = {
      "track_a": _track_a_lane(loop, cfg),
      "track_b_shadow": track_b,
      "slot15_eth": _slot15_eth_lane(loop, cfg),
    }
  except sqlite3.OperationalError as exc:
    if "locked" in str(exc).lower():
      log.warning("four_k_week_plan: db busy — %s", exc)
      raise
    raise
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
    "kalshi_wallet": kalshi_wallet,
    "lanes": lanes,
    "track_b_epoch_at": track_b_epoch_iso(cfg),
    "probe_stats_epoch_at": probe_stats_epoch_iso(cfg),
  }


def track_b_settled_count_fast(cfg: dict[str, Any] | None, *, asset: str = "eth") -> int:
  """Lightweight settled-event count for revision polling (no full summarize)."""
  if not track_b_shadow_active(cfg):
    return 0
  epoch_iso = track_b_epoch_iso(cfg)
  root = shadow_log_dir(cfg)
  count = 0
  for path in sorted(root.glob("*.jsonl")):
    try:
      text = path.read_text(encoding="utf-8")
    except OSError:
      continue
    for line in text.splitlines():
      if not line.strip():
        continue
      try:
        row = json.loads(line)
      except json.JSONDecodeError:
        continue
      if str(row.get("asset") or "").lower() != str(asset).lower():
        continue
      if row.get("type") != "settlement":
        continue
      if _epoch_ok(str(row.get("ts") or ""), epoch_iso):
        count += 1
  return count


def four_k_week_plan_revision(loop: Any, cfg: dict[str, Any] | None) -> dict[str, Any]:
  """Cheap closed-trade counters — dashboard refreshes full plan only when this changes."""
  if not four_k_week_plan_active(cfg):
    return {"ok": False, "enabled": False}

  from src.trading.compare_paper_twins import compare_store_kinds

  asset = "eth"
  live_kind, trial_kind = compare_store_kinds(asset)
  live_store = loop.hourly_bot_store(asset, kind=live_kind)
  trial_store = loop.hourly_bot_store(asset, kind=trial_kind)
  epoch = effective_compare_stats_epoch_at(live_store, cfg)
  epoch_dt = parse_stats_epoch_at(epoch)

  live_stats = _lane_pnl_from_store(live_store, mode="live", since=epoch_dt, trade_limit=200)
  live_stats.pop("_entry_events", None)
  trial_mode = str(trial_store.get_settings().mode or "paper").lower()
  trial_stats = _lane_pnl_from_store(trial_store, mode=trial_mode, since=epoch_dt, trade_limit=200)
  trial_stats.pop("_entry_events", None)

  since = eth_slot15_experiment_start_at(cfg)
  slot_store = loop.slot15_bot_store("eth")
  slot_mode = str(slot_store.get_settings().mode or "paper").lower()
  slot_stats = _lane_pnl_from_store(slot_store, mode=slot_mode, since=since, trade_limit=200)
  slot_stats.pop("_entry_events", None)

  settled = track_b_settled_count_fast(cfg, asset="eth")

  lanes = {
    "track_a_live_exits": int(live_stats.get("exits") or 0),
    "track_a_trial_exits": int(trial_stats.get("exits") or 0),
    "track_b_settled_events": settled,
    "slot15_exits": int(slot_stats.get("exits") or 0),
  }
  revision = "|".join(str(lanes[k]) for k in (
    "track_a_live_exits",
    "track_a_trial_exits",
    "track_b_settled_events",
    "slot15_exits",
  ))
  return {"ok": True, "revision": revision, "lanes": lanes}


def four_k_week_plan_revision_cached(
  loop: Any,
  cfg: dict[str, Any] | None,
  *,
  ttl_sec: float = _REVISION_CACHE_TTL_SEC,
) -> dict[str, Any]:
  """Return cached revision counters when fresh (revision poll is high-frequency)."""
  now = time.monotonic()
  cached = _REVISION_CACHE.get("payload")
  if cached and (now - float(_REVISION_CACHE.get("mono_at") or 0)) < ttl_sec:
    return {**cached, "cached": True, "cache_age_sec": round(now - float(_REVISION_CACHE["mono_at"]), 1)}

  try:
    payload = four_k_week_plan_revision(loop, cfg)
  except sqlite3.OperationalError as exc:
    if "locked" in str(exc).lower() and cached:
      return {
        **cached,
        "cached": True,
        "stale": True,
        "stale_reason": "db_busy",
        "error": str(exc),
      }
    raise

  _REVISION_CACHE["mono_at"] = now
  _REVISION_CACHE["payload"] = payload
  return {**payload, "cached": False, "cache_age_sec": 0.0}


def build_four_k_week_plan_report_cached(
  loop: Any,
  cfg: dict[str, Any] | None,
  *,
  ttl_sec: float = _REPORT_CACHE_TTL_SEC,
) -> dict[str, Any]:
  """Return cached plan summary when fresh to avoid SQLite lock storms."""
  now = time.monotonic()
  cached = _REPORT_CACHE.get("payload")
  if cached and (now - float(_REPORT_CACHE.get("mono_at") or 0)) < ttl_sec:
    return {**cached, "cached": True, "cache_age_sec": round(now - float(_REPORT_CACHE["mono_at"]), 1)}

  try:
    payload = build_four_k_week_plan_report(loop, cfg)
  except sqlite3.OperationalError as exc:
    if "locked" in str(exc).lower() and cached:
      return {
        **cached,
        "cached": True,
        "stale": True,
        "stale_reason": "db_busy",
        "error": str(exc),
      }
    raise

  _REPORT_CACHE["mono_at"] = now
  _REPORT_CACHE["payload"] = payload
  return {**payload, "cached": False, "cache_age_sec": 0.0}


def invalidate_four_k_week_plan_cache() -> None:
  _REPORT_CACHE["mono_at"] = 0.0
  _REPORT_CACHE["payload"] = None
  _REVISION_CACHE["mono_at"] = 0.0
  _REVISION_CACHE["payload"] = None


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
