"""24/7 P&L-first manager on Railway — sleep lock, preflight, milestone, status."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_STATE: dict[str, Any] = {"cycle": 0}


@dataclass(frozen=True)
class PnlFirstManagerConfig:
  enabled: bool = True
  interval_seconds: int = 30
  phase: str = "prep"
  enforce_sleep: bool = True
  trading_armed: bool = False
  auto_wake_when_ready: bool = False
  owner_poa_live: bool = False
  owner_poa_debug_cap_usd: float = 15.0
  live_cap_usd: float = 30.0
  milestone_interval_minutes: int = 60
  auto_sync_on_reconcile: bool = True
  lock_eth_live: bool = True
  lock_slot15: bool = True
  allow_eth_paper: bool = False
  allow_eth_live: bool = False
  allow_eth_slot15_paper: bool = False
  allow_btc_live: bool = False
  allow_index_paper: bool = False

  @classmethod
  def from_cfg(cls, cfg: dict[str, Any] | None) -> PnlFirstManagerConfig:
    raw = dict((cfg or {}).get("pnl_first_manager") or {})
    return cls(
      enabled=bool(raw.get("enabled", True)),
      interval_seconds=int(raw.get("interval_seconds", 30)),
      phase=str(raw.get("phase", "prep")).lower(),
      enforce_sleep=bool(raw.get("enforce_sleep", True)),
      trading_armed=bool(raw.get("trading_armed", False)),
      auto_wake_when_ready=bool(raw.get("auto_wake_when_ready", False)),
      owner_poa_live=bool(raw.get("owner_poa_live", False)),
      owner_poa_debug_cap_usd=float(raw.get("owner_poa_debug_cap_usd", 15.0)),
      live_cap_usd=float(raw.get("live_cap_usd", 30.0)),
      milestone_interval_minutes=int(raw.get("milestone_interval_minutes", 60)),
      auto_sync_on_reconcile=bool(raw.get("auto_sync_on_reconcile", True)),
      lock_eth_live=bool(raw.get("lock_eth_live", True)),
      lock_slot15=bool(raw.get("lock_slot15", True)),
      allow_eth_paper=bool(raw.get("allow_eth_paper", False)),
      allow_eth_live=bool(raw.get("allow_eth_live", False)),
      allow_eth_slot15_paper=bool(raw.get("allow_eth_slot15_paper", False)),
      allow_btc_live=bool(raw.get("allow_btc_live", False)),
      allow_index_paper=bool(raw.get("allow_index_paper", False)),
    )


def manager_log_dir(cfg: dict[str, Any] | None = None) -> Path:
  del cfg
  base = Path(os.getenv("DATA_DIR", "data"))
  d = base / "logs" / "pnl_first_manager"
  d.mkdir(parents=True, exist_ok=True)
  return d


def _state_path(cfg: dict[str, Any] | None) -> Path:
  return manager_log_dir(cfg) / "manager_state.json"


def load_manager_state(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
  path = _state_path(cfg)
  if not path.exists():
    return {}
  try:
    return json.loads(path.read_text(encoding="utf-8"))
  except Exception:
    return {}


def save_manager_state(state: dict[str, Any], cfg: dict[str, Any] | None = None) -> None:
  path = _state_path(cfg)
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")


def _stats_epoch(cfg: dict[str, Any] | None) -> datetime:
  raw = dict((cfg or {}).get("pnl_first") or {}).get("phase_started_at")
  if not raw:
    raw = dict(((cfg or {}).get("hourly") or {}).get("bot") or {}).get("experiment_start_at")
  if raw:
    try:
      return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
      pass
  return datetime(2026, 7, 4, 16, 59, tzinfo=timezone.utc)


def experiment_epoch_at(loop: Any, cfg: dict[str, Any] | None, *, asset: str = "btc") -> datetime:
  """Single epoch for Kalshi vs bot comparisons — prefers bot store stats_epoch_at."""
  from src.trading.bot_runtime import parse_stats_epoch_at, stats_epoch_at

  try:
    store = loop.hourly_bot_store(asset, kind="hourly")
    with store._connect() as conn:
      raw = stats_epoch_at(conn)
    parsed = parse_stats_epoch_at(raw)
    if parsed is not None:
      return parsed
  except Exception:
    pass
  return _stats_epoch(cfg)


def _poa_live_active(cfg: dict[str, Any] | None) -> bool:
  return bool(load_manager_state(cfg).get("poa_live_active"))


def _poa_exercise_requested(cfg: dict[str, Any] | None) -> bool:
  """Set after owner ping timeout — owner_poa_live alone does not auto-wake."""
  return bool(load_manager_state(cfg).get("poa_exercise_requested"))


def enforce_sleep_lock(loop: Any, mgr: PnlFirstManagerConfig) -> list[dict[str, Any]]:
  """Keep auto-bet off during prep unless trading is explicitly armed (BTC hourly POA only)."""
  if not mgr.enforce_sleep or mgr.phase != "prep" or mgr.trading_armed:
    return []

  from src.trading.hourly_bot_store import HourlyBotSettings
  from src.trading.slot15_bot_store import Slot15BotSettings

  cfg = loop.cfg
  btc_poa_awake = _poa_live_active(cfg)
  actions: list[dict[str, Any]] = []

  # (asset, kind, store_fn_name) — kind is hourly | slot15 | slot15_trial
  targets: list[tuple[str, str, str]] = [("btc", "hourly", "hourly_bot_store")]
  if mgr.lock_slot15:
    targets.append(("btc", "slot15", "slot15_bot_store"))
    targets.append(("btc", "slot15_trial", "slot15_trial_bot_store"))
  if mgr.lock_eth_live or mgr.allow_eth_paper or mgr.allow_eth_live:
    targets.extend([
      ("eth", "hourly", "hourly_bot_store"),
    ])
    from src.trading.eth_paper_experiment import eth_live_mirror_active

    if mgr.allow_eth_live and eth_live_mirror_active(cfg):
      targets.append(("eth", "hourly_live", "hourly_bot_store"))
    if mgr.lock_slot15:
      targets.extend([
        ("eth", "slot15", "slot15_bot_store"),
        ("eth", "slot15_trial", "slot15_trial_bot_store"),
      ])

  for asset, kind, store_fn in targets:
    # POA live session: do not re-disable BTC hourly — unless twin live is managing it
    if asset == "btc" and kind == "hourly" and btc_poa_awake and not mgr.allow_btc_live:
      continue
    store = (
      loop.hourly_bot_store(asset, kind=kind)
      if store_fn == "hourly_bot_store"
      else getattr(loop, store_fn)(asset)
    )
    settings = store.get_settings()
    changed = False
    updates = dict(settings.to_dict())
    eth_paper_exempt = False
    eth_live_exempt = False
    eth_slot15_paper_exempt = False
    btc_live_exempt = False
    eth_bot_cfg_yaml = dict(
      (((cfg or {}).get("eth") or {}).get("hourly") or {}).get("bot") or {}
    )
    btc_bot_cfg_yaml = dict((((cfg or {}).get("hourly") or {}).get("bot") or {}))
    if asset == "btc" and kind == "hourly" and mgr.allow_btc_live:
      from src.trading.btc_twin_live import btc_twin_live_active

      if btc_twin_live_active(cfg):
        btc_live_exempt = True
    if asset == "eth" and kind == "hourly" and mgr.allow_eth_paper:
      if (
        bool(eth_bot_cfg_yaml.get("enabled"))
        and str(eth_bot_cfg_yaml.get("mode") or "").lower() == "paper"
      ):
        eth_paper_exempt = True
    if asset == "eth" and kind == "hourly_live" and mgr.allow_eth_live:
      from src.trading.eth_paper_experiment import eth_live_mirror_active

      if eth_live_mirror_active(cfg):
        eth_live_exempt = True
    if asset == "eth" and kind.startswith("slot15") and mgr.allow_eth_slot15_paper:
      slot15_cfg = dict(
        ((((cfg or {}).get("eth") or {}).get("intra_slot") or {}).get("bot") or {})
      )
      if (
        bool(slot15_cfg.get("enabled", True))
        and str(slot15_cfg.get("mode") or "paper").lower() == "paper"
      ):
        eth_slot15_paper_exempt = True
    if eth_paper_exempt:
      if not settings.enabled:
        updates["enabled"] = True
        changed = True
      if not settings.continuous and bool(eth_bot_cfg_yaml.get("continuous_enabled", True)):
        updates["continuous"] = True
        changed = True
      if str(settings.mode or "").lower() != "paper":
        updates["mode"] = "paper"
        changed = True
    elif eth_live_exempt:
      mirror = dict(eth_bot_cfg_yaml.get("live_mirror") or {})
      if not settings.enabled:
        updates["enabled"] = True
        changed = True
      if not settings.continuous and bool(
        mirror.get("continuous_enabled", eth_bot_cfg_yaml.get("continuous_enabled", True))
      ):
        updates["continuous"] = True
        changed = True
      if str(settings.mode or "").lower() != "live":
        updates["mode"] = "live"
        changed = True
    elif btc_live_exempt:
      twin = dict(btc_bot_cfg_yaml.get("twin_live") or {})
      if not settings.enabled:
        updates["enabled"] = True
        changed = True
      if not settings.continuous and bool(
        twin.get("continuous_enabled", btc_bot_cfg_yaml.get("continuous_enabled", True))
      ):
        updates["continuous"] = True
        changed = True
      if str(settings.mode or "").lower() != "live":
        updates["mode"] = "live"
        changed = True
    elif eth_slot15_paper_exempt:
      slot15_cfg = dict(
        ((((cfg or {}).get("eth") or {}).get("intra_slot") or {}).get("bot") or {})
      )
      if not settings.enabled:
        updates["enabled"] = True
        changed = True
      if not settings.continuous and bool(slot15_cfg.get("continuous_enabled", True)):
        updates["continuous"] = True
        changed = True
      if str(settings.mode or "").lower() != "paper":
        updates["mode"] = "paper"
        changed = True
    elif settings.enabled:
      updates["enabled"] = False
      changed = True
    if (
      mgr.lock_eth_live
      and asset == "eth"
      and kind == "hourly"
      and settings.mode == "live"
      and not eth_live_exempt
    ):
      updates["mode"] = "paper"
      changed = True
    if mgr.lock_slot15 and kind.startswith("slot15") and settings.mode == "live":
      updates["mode"] = "paper"
      changed = True
    if changed:
      if kind in ("hourly", "hourly_live"):
        store.save_settings(
          HourlyBotSettings.from_dict(updates),
          source="pnl_first_railway_manager_sleep_lock",
        )
      else:
        store.save_settings(
          Slot15BotSettings.from_dict(updates),
          source="pnl_first_railway_manager_sleep_lock",
        )
      actions.append({
        "action": (
          "eth_paper_arm"
          if eth_paper_exempt
          else "eth_live_arm"
          if eth_live_exempt
          else "eth_slot15_paper_arm"
          if eth_slot15_paper_exempt
          else "btc_live_arm"
          if btc_live_exempt
          else "sleep_lock"
        ),
        "asset": asset,
        "kind": kind,
      })
      log.info(
        "pnl_first_manager: sleep lock %s/%s enabled=%s mode=%s",
        asset, kind, updates.get("enabled"), updates.get("mode"),
      )

    if eth_paper_exempt and asset == "eth" and kind == "hourly":
      from src.trading.eth_paper_experiment import seed_eth_paper_settings_from_cfg

      seed_result = seed_eth_paper_settings_from_cfg(
        store,
        cfg,
        source="pnl_first_manager_eth_paper_arm",
      )
      if seed_result.get("synced"):
        actions.append({"action": "eth_paper_settings_sync", **seed_result})

    if eth_live_exempt and asset == "eth" and kind == "hourly_live":
      from src.trading.eth_paper_experiment import seed_eth_live_mirror_from_cfg

      seed_result = seed_eth_live_mirror_from_cfg(
        store,
        cfg,
        source="pnl_first_manager_eth_live_arm",
      )
      if seed_result.get("synced"):
        actions.append({"action": "eth_live_settings_sync", **seed_result})

    if btc_live_exempt and asset == "btc" and kind == "hourly":
      from src.trading.btc_twin_live import seed_btc_twin_live_from_cfg

      seed_result = seed_btc_twin_live_from_cfg(
        store,
        cfg,
        source="pnl_first_manager_btc_live_arm",
      )
      if seed_result.get("synced"):
        actions.append({"action": "btc_live_settings_sync", **seed_result})
  return actions


def compute_btc_live_trade_timing(loop: Any, cfg: dict[str, Any] | None) -> dict[str, Any]:
  """Live BTC hourly PnL by minutes-to-settle at entry (experiment window)."""
  from src.trading.bot_performance_report import experiment_start_at
  from src.trading.trade_timing_analytics import build_trade_timing_report

  store = loop.hourly_bot_store("btc", kind="hourly")
  trades = store.list_trades(limit=2000)
  since = experiment_start_at(cfg)
  report = build_trade_timing_report(trades, mode="live", since=since)
  report["experiment_start_at"] = since.isoformat() if since else None
  return report


def run_preflight(loop: Any, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
  issues: list[str] = []
  detail: dict[str, Any] = {}

  tab = loop.daily_prediction()
  event = (tab.get("event") or {}).get("event_ticker")
  detail["event_ticker"] = event
  detail["hourly_tab_ok"] = bool(tab.get("ok"))
  if not tab.get("ok") or not event:
    issues.append("hourly_tab_unavailable")

  store = loop.hourly_bot_store("btc")
  settings = store.get_settings()
  detail["btc_enabled"] = settings.enabled
  detail["btc_mode"] = settings.mode
  open_pos = (
    store.all_open_live_positions()
    if settings.mode == "live" and hasattr(store, "all_open_live_positions")
    else (store.open_positions(event) if event else [])
  )
  detail["open_legs"] = len(open_pos or [])
  detail["open_exposure_usd"] = sum(float(p.get("cost_usd") or 0) for p in (open_pos or []))
  if detail["open_legs"] or float(detail["open_exposure_usd"] or 0) > 0.01:
    issues.append("bot_has_open_legs")

  try:
    recon = loop.hourly_live_reconcile("btc")
    detail["reconcile_event"] = recon.get("event_ticker")
    detail["kalshi_only"] = len(recon.get("kalshi_only") or [])
    detail["bot_only"] = len(recon.get("bot_only") or [])
    if detail["bot_only"]:
      issues.append("reconcile_bot_only")
    if event and recon.get("event_ticker") == event and detail["kalshi_only"]:
      issues.append(f"reconcile_kalshi_only:{detail['kalshi_only']}")
  except Exception as exc:
    issues.append(f"reconcile_error:{exc}")

  kalshi = loop.kalshi
  if kalshi and getattr(kalshi, "authenticated", False):
    from src.trading.kalshi_fill_sync import summarize_kalshi_experiment_fills

    since = _stats_epoch(cfg)
    sm = summarize_kalshi_experiment_fills(kalshi, since=since, asset="btc")
    detail["kalshi_fill_summary"] = {
      k: sm.get(k)
      for k in (
        "closed_trades",
        "fills_scanned",
        "pairable_legs",
        "post_epoch_buys",
        "post_epoch_sells",
        "post_epoch_settlements",
        "settlement_exits",
        "total_pnl_usd",
      )
    }
    if (
      sm.get("ok")
      and int(sm.get("closed_trades") or 0) == 0
      and int(sm.get("fills_scanned") or 0) > 20
      and (
        int(sm.get("post_epoch_sells") or 0) > 0
        or int(sm.get("post_epoch_settlements") or 0) > 0
      )
    ):
      issues.append("kalshi_fill_summary_zero_closed")

  from src.backtest.mechanics_profiles import live_mechanics_profile_for_cfg
  from src.trading.live_entry_guard_summary import build_live_entry_guard_summary

  acfg = loop.cfg
  gs = build_live_entry_guard_summary(acfg, mode="live", kind="hourly", asset="btc")
  detail["mechanics_profile"] = live_mechanics_profile_for_cfg(acfg) or gs.get("mechanics_profile")
  if detail["mechanics_profile"] != "pnl_first":
    issues.append(f"profile_not_pnl_first:{detail['mechanics_profile']}")

  return {
    "ok": not issues,
    "issues": issues,
    "detail": detail,
    "ts": datetime.now(timezone.utc).isoformat(),
  }


def compute_live_milestone(
  loop: Any,
  cfg: dict[str, Any] | None = None,
  *,
  target_hours: int | None = None,
) -> dict[str, Any]:
  from src.trading.pnl_first_pipeline_milestone import (
    compute_pipeline_milestone,
    sync_pipeline_hour_boundary,
  )

  preflight_event = None
  try:
    tab = loop.daily_prediction()
    if tab.get("ok"):
      preflight_event = (tab.get("event") or {}).get("event_ticker")
  except Exception:
    pass
  if preflight_event:
    sync_pipeline_hour_boundary(cfg, str(preflight_event))

  out = compute_pipeline_milestone(cfg)
  if target_hours is not None:
    out["target_pipeline_hours"] = target_hours
    out["target_positive_hours"] = target_hours
  return out


def try_controlled_wake(loop: Any, mgr: PnlFirstManagerConfig, preflight: dict[str, Any]) -> dict[str, Any] | None:
  poa_exercise = mgr.owner_poa_live and _poa_exercise_requested(loop.cfg)
  may_wake = mgr.trading_armed or mgr.auto_wake_when_ready or poa_exercise
  if not may_wake:
    return None
  if not preflight.get("ok"):
    return None

  from src.trading.hourly_bot_store import HourlyBotSettings

  store = loop.hourly_bot_store("btc")
  settings = store.get_settings()
  if settings.enabled and settings.mode == "live":
    return {"action": "already_awake"}

  tab = loop.daily_prediction()
  live = tab.get("live") or tab
  regime = live.get("regime") or {}
  if regime.get("blocked") or regime.get("allow_trade") is False:
    return {"action": "wake_deferred", "reason": "regime_blocked"}

  poa_wake = poa_exercise and not mgr.trading_armed and not mgr.auto_wake_when_ready
  cap = mgr.owner_poa_debug_cap_usd if poa_wake else mgr.live_cap_usd
  updated = HourlyBotSettings.from_dict({
    **settings.to_dict(),
    "enabled": True,
    "mode": "live",
    "max_spend_per_hour_usd": cap,
    "allow_strong": False,
    "allow_actionable": False,
  })
  store.save_settings(updated, source="pnl_first_railway_manager_wake_poa" if poa_wake else "pnl_first_railway_manager_wake")
  if poa_wake:
    save_manager_state({
      **load_manager_state(loop.cfg),
      "poa_live_active": True,
      "poa_wake_at": datetime.now(timezone.utc).isoformat(),
      "poa_debug_cap_usd": cap,
    }, loop.cfg)
  log.info(
    "pnl_first_manager: %s wake BTC live cap=$%.2f",
    "POA" if poa_wake else "controlled",
    cap,
  )
  return {
    "action": "wake_poa" if poa_wake else "wake",
    "asset": "btc",
    "cap_usd": cap,
    "owner_poa": poa_wake,
  }


def maybe_sync_reconcile(loop: Any, mgr: PnlFirstManagerConfig, preflight: dict[str, Any]) -> dict[str, Any] | None:
  if not mgr.auto_sync_on_reconcile:
    return None
  issues = preflight.get("issues") or []
  if not any(str(i).startswith("reconcile_") for i in issues):
    return None
  try:
    from src.trading.kalshi_fill_sync import sync_kalshi_fills_to_store

    store = loop.hourly_bot_store("btc")
    result = sync_kalshi_fills_to_store(store, loop.kalshi, force=True, asset="btc")
    return {"action": "sync_kalshi_fills", "result": result}
  except Exception as exc:
    log.warning("pnl_first_manager sync failed: %s", exc)
    return {"action": "sync_failed", "error": str(exc)}


def run_manager_tick(loop: Any) -> dict[str, Any]:
  """Single manager cycle — called from APScheduler on Railway."""
  cfg = loop.cfg
  mgr = PnlFirstManagerConfig.from_cfg(cfg)
  if not mgr.enabled:
    return {"skipped": True}

  global _STATE
  _STATE["cycle"] = int(_STATE.get("cycle") or 0) + 1
  cycle = _STATE["cycle"]

  actions: list[dict[str, Any]] = []
  actions.extend(enforce_sleep_lock(loop, mgr))

  if mgr.allow_index_paper:
    try:
      from src.trading.index_paper_experiment import ensure_index_paper_experiments

      index_boot = ensure_index_paper_experiments(loop)
      for asset, result in (index_boot.get("assets") or {}).items():
        if result.get("synced") and result.get("changed_fields"):
          actions.append({"action": "index_paper_settings_sync", "asset": asset, **result})
    except Exception as exc:
      log.warning("index paper experiment ensure failed: %s", exc)

  try:
    from src.trading.compare_paper_twins import ensure_compare_paper_twins

    twin_result = ensure_compare_paper_twins(loop, cfg)
    if twin_result.get("active"):
      changed = [
        t for t in (twin_result.get("twins") or [])
        if t.get("changed_fields")
      ]
      if changed:
        actions.append({"action": "compare_paper_twins", "twins": changed})
  except Exception as exc:
    log.warning("compare_paper_twins ensure failed: %s", exc)

  preflight = run_preflight(loop, cfg)
  from src.trading.pnl_first_pipeline_milestone import note_pipeline_preflight

  note_pipeline_preflight(cfg, ok=bool(preflight.get("ok")))
  wake = try_controlled_wake(loop, mgr, preflight)
  if wake:
    actions.append(wake)
  sync = maybe_sync_reconcile(loop, mgr, preflight)
  if sync:
    actions.append(sync)

  from src.trading.pnl_first_backtest_runner import (
    backtest_status,
    maybe_sync_kalshi_periodic,
    run_live_pnl_audit,
    tick_backtest_runner,
  )

  periodic_sync = maybe_sync_kalshi_periodic(loop, cycle, every=15)
  if periodic_sync:
    actions.append({"action": "periodic_kalshi_sync", "result": periodic_sync})

  live_audit = run_live_pnl_audit(loop, cfg)
  if live_audit.get("issues"):
    actions.append({"action": "live_pnl_audit", "issues": live_audit["issues"]})

  bt = tick_backtest_runner(cfg)
  if bt:
    actions.append(bt)

  from src.trading.pnl_first_backtest_runner import backtest_status
  from src.trading.pnl_first_health_watchdog import run_health_watchdog

  bt_status = backtest_status(cfg)
  health = run_health_watchdog(loop, cfg, jobs=bt_status.get("jobs"))
  if not health.get("ok"):
    actions.append({"action": "health_issues", "issues": health.get("issues")})

  milestone: dict[str, Any] | None = None
  last_milestone_at = _STATE.get("last_milestone_at")
  now = datetime.now(timezone.utc)
  due_milestone = True
  if last_milestone_at:
    try:
      last = datetime.fromisoformat(str(last_milestone_at).replace("Z", "+00:00"))
      due_milestone = (now - last).total_seconds() >= mgr.milestone_interval_minutes * 60
    except ValueError:
      pass
  if due_milestone or cycle == 1:
    milestone = compute_live_milestone(loop, cfg)
    _STATE["last_milestone_at"] = now.isoformat()

  trade_timing: dict[str, Any] | None = None
  if due_milestone or cycle == 1:
    trade_timing = compute_btc_live_trade_timing(loop, cfg)
    try:
      from src.trading.kalshi_live_report import build_kalshi_live_report

      kalshi_live = build_kalshi_live_report(loop, cfg, asset="btc")
      if kalshi_live.get("ok"):
        (manager_log_dir(cfg) / "kalshi_live_report_latest.json").write_text(
          json.dumps(kalshi_live, indent=2, default=str),
          encoding="utf-8",
        )
    except Exception as exc:
      log.warning("kalshi live report failed: %s", exc)
      kalshi_live = {"ok": False, "error": str(exc)}
    try:
      from src.trading.pnl_first_paper_ab import write_paper_ab_report

      write_paper_ab_report(loop, cfg)
      actions.append({"action": "paper_ab_report", "ok": True})
    except Exception as exc:
      actions.append({"action": "paper_ab_report", "ok": False, "error": str(exc)})
  else:
    kalshi_live = None

  report = {
    "ts": now.isoformat(),
    "cycle": cycle,
    "phase": mgr.phase,
    "trading_armed": mgr.trading_armed,
    "owner_poa_live": mgr.owner_poa_live,
    "poa_live_active": _poa_live_active(cfg),
    "preflight": preflight,
    "milestone": milestone,
    "live_audit": live_audit,
    "trade_timing": trade_timing,
    "health": health,
    "kalshi_live": kalshi_live,
    "backtest": bt_status,
    "actions": actions,
  }

  log_dir = manager_log_dir(cfg)
  status_path = log_dir / "status.json"
  status_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

  if cycle % 20 == 0 or actions or not preflight.get("ok"):
    log.info(
      "pnl_first_manager cycle=%s preflight_ok=%s issues=%s actions=%s streak=%s/%s",
      cycle,
      preflight.get("ok"),
      len(preflight.get("issues") or []),
      [a.get("action") for a in actions],
      (milestone or {}).get("consecutive_pipeline_hours"),
      (milestone or {}).get("target_pipeline_hours"),
    )

  if milestone and milestone.get("milestone_achieved"):
    achieved_path = log_dir / "milestone_ACHIEVED.json"
    achieved_path.write_text(json.dumps(milestone, indent=2), encoding="utf-8")
    log.info(
      "pnl_first_manager MILESTONE ACHIEVED: %s pipeline hours, gates=%s",
      milestone.get("consecutive_pipeline_hours"),
      milestone.get("session_gate_coverage"),
    )

  save_manager_state({**load_manager_state(cfg), "last_report": report}, cfg)
  _STATE["last_report"] = report
  return report


def manager_status_snapshot(loop: Any | None = None) -> dict[str, Any]:
  if loop is None:
    return dict(_STATE)
  cfg = loop.cfg
  state = load_manager_state(cfg)
  snap = dict(_STATE)
  snap["persisted"] = state
  if (log_dir := manager_log_dir(cfg)).joinpath("status.json").exists():
    try:
      snap["last_status_file"] = json.loads((log_dir / "status.json").read_text(encoding="utf-8"))
    except Exception:
      pass
  return snap
