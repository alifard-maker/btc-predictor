#!/usr/bin/env python3
"""2h manager monitor — BTC/ETH hourly S1/S2 live + trial paper bots vs Kalshi.

Each cycle analyzes the **interval since the last cycle** (default 30s), not only
cumulative 2h lookback. See .cursor/rules/live-monitor.mdc for the full protocol.

Usage:
  python scripts/run_2h_manager_watchdog.py   # preferred: 2h + auto-restart
  python scripts/monitor_hourly_s1_s2_manager.py --duration-minutes 120 --interval 30
  python scripts/monitor_hourly_s1_s2_manager.py --once
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
  sys.path.insert(0, str(ROOT))

DEFAULT_BASE = os.environ.get(
  "BTC_PREDICTOR_URL",
  "https://btc-predictor-production-f460.up.railway.app",
)
DEFAULT_STATS_EPOCH = "2026-07-04T16:59:00+00:00"
LOG_DIR = ROOT / "data" / "logs" / "manager_monitor"
LAST_CYCLE_STATE = LOG_DIR / "last_cycle_state.json"

S1_RE = re.compile(r"-T\d", re.I)
S2_RE = re.compile(r"-B\d", re.I)

# Trial paper bots polled each cycle (path suffix after base URL)
TRIAL_BOTS: tuple[tuple[str, str, str], ...] = (
  ("btc", "standard", "/api/hourly-trial/bot"),
  ("btc", "mech", "/api/hourly-trial-mech/bot"),
  ("btc", "rally", "/api/hourly-trial-rally/bot"),
  ("btc", "soft", "/api/hourly-trial-soft/bot"),
  ("eth", "standard", "/api/eth/hourly-trial/bot"),
)

LIVE_IDLE_SKIP_MARKERS = (
  "adaptive_throttle",
  "adaptive_bucket_paused",
  "hour_momentum",
  "regime_blocked",
  "whipsaw",
  "too_far",
  "ask_edge",
  "budget",
)

# Abnormal-bet thresholds (user asked manager to catch these proactively)
MAX_NORMAL_LIVE_CONTRACTS = 8
LATE_HOUR_MINUTES_TO_SETTLE = 7
INTERVAL_RED_BLOCK_USD = -3.0
EXIT_SPAM_WINDOW_S = 120
EXIT_SPAM_MIN_COUNT = 5


def _password() -> str:
  env_path = ROOT / ".env"
  if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
      if line.startswith("APP_PASSWORD="):
        return line.split("=", 1)[1].strip()
  return os.environ.get("APP_PASSWORD", "")


def _login(session: requests.Session, base: str, password: str) -> None:
  r = session.post(f"{base}/api/auth/login", data={"password": password}, timeout=30)
  if r.status_code not in (200, 302, 303):
    raise RuntimeError(f"login failed: {r.status_code} {r.text[:200]}")


def _strategy(ticker: str | None) -> str:
  t = str(ticker or "")
  if S2_RE.search(t):
    return "S2"
  if S1_RE.search(t):
    return "S1"
  return "?"


def _parse_ts(raw: str | None) -> datetime | None:
  if not raw:
    return None
  try:
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
  except ValueError:
    return None


def _hold_seconds(enter: dict[str, Any], exit_row: dict[str, Any] | None) -> float | None:
  t0 = _parse_ts(enter.get("created_at"))
  t1 = _parse_ts(exit_row.get("created_at")) if exit_row else None
  if t0 is None:
    return None
  end = t1 or datetime.now(timezone.utc)
  return (end - t0).total_seconds()


def _api_prefix(asset: str) -> str:
  return "/api/eth" if asset == "eth" else "/api"


def _get_json(session: requests.Session, url: str, *, params: dict | None = None, timeout: int = 60) -> dict[str, Any]:
  r = session.get(url, params=params, timeout=timeout)
  try:
    body = r.json()
  except Exception:
    body = {"ok": False, "error": r.text[:300], "status": r.status_code}
  if r.status_code >= 400 and "error" not in body:
    body = {"ok": False, "error": f"HTTP {r.status_code}", "body": body}
  return body


def _fetch_asset_bundle(session: requests.Session, base: str, asset: str) -> dict[str, Any]:
  p = _api_prefix(asset)
  live = _get_json(session, f"{base}{p}/hourly/bot")
  trial = _get_json(session, f"{base}{p}/hourly-trial/bot")
  recon_path = f"{base}{p}/hourly/bot/live-reconcile"
  recon = _get_json(session, recon_path)
  compare = _get_json(session, f"{base}/api/bots/hourly-live-trial-compare?asset={asset}&limit_hours=6")
  trades = _get_json(session, f"{base}{p}/hourly/bot/trades?limit=200")
  kalshi_fill = _get_json(
    session,
    f"{base}{p}/hourly/bot/kalshi-fill-summary",
    params={"since": DEFAULT_STATS_EPOCH},
  )
  return {
    "asset": asset,
    "live": live,
    "trial": trial,
    "reconcile": recon,
    "compare": compare,
    "trades": trades,
    "kalshi_fill_summary": kalshi_fill,
  }


def _fetch_trial_bot(session: requests.Session, base: str, bot_path: str) -> dict[str, Any]:
  status = _get_json(session, f"{base}{bot_path}")
  trades = _get_json(session, f"{base}{bot_path}/trades", params={"limit": 200})
  return {"status": status, "trades": trades}


def _trial_snapshot(
  *,
  asset: str,
  variant: str,
  status: dict[str, Any],
  trades: list[dict[str, Any]],
  lookback_hours: float,
) -> dict[str, Any]:
  settings = status.get("settings") or {}
  open_positions = status.get("open_positions") or []
  recent = _trades_in_lookback(trades, lookback_hours)
  paper = [t for t in recent if str(t.get("mode", "paper")).lower() == "paper"]
  enters = [t for t in paper if t.get("action") == "enter" and t.get("status") == "filled"]
  exits = [t for t in paper if t.get("action") == "exit" and t.get("status") == "filled"]
  s1_open = sum(1 for p in open_positions if _strategy(p.get("market_ticker")) == "S1")
  s2_open = sum(1 for p in open_positions if _strategy(p.get("market_ticker")) == "S2")
  return {
    "asset": asset,
    "variant": variant,
    "enabled": bool(settings.get("enabled")),
    "skip": status.get("last_skip_reason"),
    "open_s1": s1_open,
    "open_s2": s2_open,
    "open_exposure_usd": status.get("open_exposure_usd"),
    "interval_net_pnl": (status.get("interval_performance") or {}).get("net_pnl_usd"),
    "remaining_usd": status.get("remaining_usd"),
    "lookback_enters": len(enters),
    "lookback_exits": len(exits),
    "lookback_net_pnl": round(
      sum(float(t.get("pnl_usd") or 0) for t in exits if t.get("pnl_usd") is not None),
      2,
    ),
    "adaptive": (status.get("adaptive_calibration") or {}).get("buckets"),
  }


def _analyze_paper_trades(
  trades: list[dict[str, Any]],
  *,
  asset: str,
  variant: str,
  spot: float | None,
) -> list[dict[str, Any]]:
  """Paper-only trade anomalies for trial bots."""
  issues: list[dict[str, Any]] = []
  tag = f"{asset}:{variant}"
  paper = [t for t in trades if str(t.get("mode", "paper")).lower() == "paper"]
  enters = [t for t in paper if t.get("action") == "enter"]
  for ent in enters:
    ticker = str(ent.get("market_ticker") or "")
    strat = _strategy(ticker)
    side = str(ent.get("side") or "").lower()
    detail = str(ent.get("detail") or "")
    label = str(ent.get("label") or ticker)
    floor = ent.get("floor_strike")
    if strat == "S2" and side == "yes" and floor is not None and spot is not None:
      try:
        gap = float(floor) - float(spot)
        if gap > 50:
          issues.append({
            "severity": "high",
            "code": "trial_s2_yes_below_spot",
            "asset": tag,
            "strat": strat,
            "label": label,
            "gap_usd": round(gap, 0),
            "at": ent.get("created_at"),
          })
      except (TypeError, ValueError):
        pass
    if "ask_edge=" in detail:
      m = re.search(r"ask_edge=(\d+)", detail)
      if m and int(m.group(1)) >= 20 and strat == "S2":
        issues.append({
          "severity": "medium",
          "code": "trial_s2_high_ask_edge",
          "asset": tag,
          "label": label,
          "ask_edge_c": int(m.group(1)),
          "at": ent.get("created_at"),
        })
  return issues


def _trial_watch_issues(
  *,
  asset: str,
  live_skip: str | None,
  trial_snap: dict[str, Any],
) -> list[dict[str, Any]]:
  """Flag when trial paper is active while live is blocked — shadow hour for comparison."""
  issues: list[dict[str, Any]] = []
  tag = f"{asset}:{trial_snap.get('variant')}"
  skip_l = str(live_skip or "").lower()
  live_idle = any(m in skip_l for m in LIVE_IDLE_SKIP_MARKERS)
  trial_enabled = trial_snap.get("enabled")
  enters = int(trial_snap.get("lookback_enters") or 0)
  if not trial_enabled:
    issues.append({
      "severity": "info",
      "code": "trial_auto_bet_off",
      "asset": tag,
      "hint": "enable Auto-bet on trial card to shadow live during throttle hours",
    })
    return issues
  if live_idle and enters == 0:
    issues.append({
      "severity": "low",
      "code": "trial_idle_while_live_blocked",
      "asset": tag,
      "live_skip": live_skip,
      "trial_skip": trial_snap.get("skip"),
    })
  elif live_idle and enters > 0:
    issues.append({
      "severity": "info",
      "code": "trial_shadowing_live_block",
      "asset": tag,
      "live_skip": live_skip,
      "lookback_enters": enters,
      "lookback_net_pnl": trial_snap.get("lookback_net_pnl"),
    })
  elif enters > 0:
    issues.append({
      "severity": "info",
      "code": "trial_paper_active",
      "asset": tag,
      "lookback_enters": enters,
      "open_s1": trial_snap.get("open_s1"),
      "open_s2": trial_snap.get("open_s2"),
      "interval_net_pnl": trial_snap.get("interval_net_pnl"),
    })
  return issues


def _minutes_to_event_settle(event_ticker: str | None, at: datetime | None) -> float | None:
  if not event_ticker or at is None:
    return None
  try:
    from src.trading.hourly_event_time import hourly_event_settle_utc

    settle = hourly_event_settle_utc(str(event_ticker))
    if settle is None:
      return None
    return (settle - at).total_seconds() / 60.0
  except Exception:
    return None


def _analyze_exit_spam(trades: list[dict[str, Any]], *, asset: str) -> list[dict[str, Any]]:
  """Flag repeated failed/resting exit attempts on the same leg."""
  issues: list[dict[str, Any]] = []
  live_exits = [
    t for t in trades
    if str(t.get("mode", "")).lower() == "live" and t.get("action") == "exit"
  ]
  by_leg: dict[str, list[datetime]] = {}
  for t in live_exits:
    leg = f"{t.get('market_ticker')}|{t.get('side')}"
    ts = _parse_ts(t.get("created_at"))
    if ts is None:
      continue
    by_leg.setdefault(leg, []).append(ts)
  now = datetime.now(timezone.utc)
  window = timedelta(seconds=EXIT_SPAM_WINDOW_S)
  for leg, times in by_leg.items():
    recent = [t for t in times if now - t <= window]
    if len(recent) >= EXIT_SPAM_MIN_COUNT:
      issues.append({
        "severity": "high",
        "code": "exit_spam",
        "asset": asset,
        "leg": leg,
        "count": len(recent),
        "window_s": EXIT_SPAM_WINDOW_S,
      })
  return issues


def _analyze_abnormal_enters(
  enters: list[dict[str, Any]],
  *,
  asset: str,
  interval_net_pnl: float | None,
) -> list[dict[str, Any]]:
  """Oversized, late-hour, and adding-while-red live entries."""
  issues: list[dict[str, Any]] = []
  for ent in enters:
    if str(ent.get("status") or "filled").lower() != "filled":
      continue
    contracts = int(ent.get("contracts") or 0)
    if contracts <= 0:
      continue
    label = str(ent.get("label") or ent.get("market_ticker") or "")
    side = str(ent.get("side") or "")
    strat = _strategy(ent.get("market_ticker"))
    at = _parse_ts(ent.get("created_at"))
    cost = float(ent.get("cost_usd") or 0)
    detail = str(ent.get("detail") or "")

    if contracts > MAX_NORMAL_LIVE_CONTRACTS:
      issues.append({
        "severity": "high",
        "code": "oversized_live_entry",
        "asset": asset,
        "strat": strat,
        "side": side,
        "contracts": contracts,
        "cost_usd": cost,
        "label": label,
        "cap": MAX_NORMAL_LIVE_CONTRACTS,
        "at": ent.get("created_at"),
      })

    evt = ent.get("event_ticker")
    mins = _minutes_to_event_settle(str(evt) if evt else None, at)
    if mins is not None and 0 < mins <= LATE_HOUR_MINUTES_TO_SETTLE:
      issues.append({
        "severity": "high",
        "code": "late_hour_live_entry",
        "asset": asset,
        "strat": strat,
        "side": side,
        "contracts": contracts,
        "min_to_settle": round(mins, 1),
        "label": label,
        "at": ent.get("created_at"),
      })

    if (
      interval_net_pnl is not None
      and interval_net_pnl <= INTERVAL_RED_BLOCK_USD
      and at is not None
      and (datetime.now(timezone.utc) - at).total_seconds() <= 1800
    ):
      issues.append({
        "severity": "high",
        "code": "live_entry_while_hour_red",
        "asset": asset,
        "strat": strat,
        "side": side,
        "contracts": contracts,
        "interval_net_pnl": interval_net_pnl,
        "label": label,
        "at": ent.get("created_at"),
      })

    if "cross_spread" in detail.lower() and contracts >= 12:
      issues.append({
        "severity": "medium",
        "code": "large_cross_spread_entry",
        "asset": asset,
        "contracts": contracts,
        "label": label,
        "at": ent.get("created_at"),
      })
  return issues


def _trades_since(trades: list[dict[str, Any]], since: datetime | None) -> list[dict[str, Any]]:
  if since is None:
    return []
  out: list[dict[str, Any]] = []
  for t in trades:
    ts = _parse_ts(t.get("created_at"))
    if ts is not None and ts > since:
      out.append(t)
  return out


def _load_last_cycle_state() -> dict[str, Any] | None:
  if not LAST_CYCLE_STATE.exists():
    return None
  try:
    return json.loads(LAST_CYCLE_STATE.read_text(encoding="utf-8"))
  except Exception:
    return None


def _save_last_cycle_state(report: dict[str, Any]) -> None:
  LOG_DIR.mkdir(parents=True, exist_ok=True)
  LAST_CYCLE_STATE.write_text(
    json.dumps(
      {"ts": report.get("ts"), "assets": report.get("assets"), "trials": report.get("trials")},
      indent=2,
      default=str,
    ),
    encoding="utf-8",
  )


def _analyze_window_delta(
  *,
  asset: str,
  window_trades: list[dict[str, Any]],
  prev_snap: dict[str, Any] | None,
  cur_snap: dict[str, Any],
  interval_net_pnl: float | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
  """Changes in the last poll interval (e.g. 30s), not just cumulative lookback."""
  issues: list[dict[str, Any]] = []
  live_w = [t for t in window_trades if str(t.get("mode", "")).lower() == "live"]
  enters = [t for t in live_w if t.get("action") == "enter" and str(t.get("status") or "") == "filled"]
  exits = [t for t in live_w if t.get("action") == "exit" and str(t.get("status") or "") == "filled"]
  window_pnl = round(sum(float(t.get("pnl_usd") or 0) for t in exits if t.get("pnl_usd") is not None), 2)

  summary: dict[str, Any] = {
    "enters": len(enters),
    "exits": len(exits),
    "window_pnl_usd": window_pnl,
    "enter_labels": [str(t.get("label") or t.get("market_ticker") or "")[:40] for t in enters[:5]],
  }

  if prev_snap:
    try:
      prev_exp = float(prev_snap.get("open_exposure_usd") or 0)
      cur_exp = float(cur_snap.get("open_exposure_usd") or 0)
      summary["exposure_delta_usd"] = round(cur_exp - prev_exp, 2)
    except (TypeError, ValueError):
      pass
    if prev_snap.get("skip") != cur_snap.get("skip"):
      summary["skip_changed"] = {"from": prev_snap.get("skip"), "to": cur_snap.get("skip")}

  if enters or exits:
    issues.extend(_analyze_abnormal_enters(enters, asset=asset, interval_net_pnl=interval_net_pnl))
    for ent in enters:
      issues.append({
        "severity": "info",
        "code": "window_live_enter",
        "asset": asset,
        "side": ent.get("side"),
        "contracts": ent.get("contracts"),
        "label": ent.get("label"),
        "at": ent.get("created_at"),
      })
    for ex in exits:
      pnl = ex.get("pnl_usd")
      if pnl is not None and float(pnl) < -0.5:
        issues.append({
          "severity": "medium",
          "code": "window_live_loss_exit",
          "asset": asset,
          "pnl_usd": pnl,
          "label": ex.get("label"),
          "at": ex.get("created_at"),
        })

  issues.extend(_analyze_exit_spam(window_trades, asset=asset))
  return issues, summary


def _validate_production(session: requests.Session, base: str) -> list[dict[str, Any]]:
  """Smoke-check bots/dashboard APIs after recent deploys (live monitor start)."""
  issues: list[dict[str, Any]] = []
  health = _get_json(session, f"{base}/health", timeout=20)
  if health.get("status") != "ok":
    issues.append({"severity": "high", "code": "health_check_failed", "asset": "app", "body": health})
  checks = [
    ("btc", "/api/hourly/bot"),
    ("eth", "/api/eth/hourly/bot"),
    ("btc_v2", "/api/hourly-v2/bot"),
    ("eth_v2", "/api/eth/hourly-v2/bot"),
    ("btc_trial", "/api/hourly-trial/bot"),
    ("eth_trial", "/api/eth/hourly-trial/bot"),
  ]
  for tag, path in checks:
    body = _get_json(session, f"{base}{path}", timeout=30)
    if body.get("error") or (body.get("ok") is False and "settings" not in body):
      issues.append({"severity": "high", "code": "bot_endpoint_failed", "asset": tag, "path": path})
  return issues


def _index_spot(bundle: dict[str, Any]) -> float | None:
  live = bundle.get("live") or {}
  for src in (
    live.get("tab") or {},
    (live.get("tab") or {}).get("live") or {},
    live,
  ):
    for key in ("current_price", "brti_live", "erti_live", "reference_price"):
      raw = src.get(key)
      if raw is not None:
        try:
          return float(raw)
        except (TypeError, ValueError):
          pass
  kalshi = live.get("kalshi") or {}
  for key in ("brti_live", "erti_live"):
    raw = kalshi.get(key)
    if raw is not None:
      try:
        return float(raw)
      except (TypeError, ValueError):
        pass
  return None


def _trades_in_lookback(trades: list[dict[str, Any]], hours: float) -> list[dict[str, Any]]:
  cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
  out: list[dict[str, Any]] = []
  for t in trades:
    ts = _parse_ts(t.get("created_at"))
    if ts is None or ts >= cutoff:
      out.append(t)
  return out


def _analyze_trades(
  trades: list[dict[str, Any]],
  *,
  asset: str,
  spot: float | None,
  open_tickers: set[str],
  min_hold_s: int = 90,
) -> list[dict[str, Any]]:
  issues: list[dict[str, Any]] = []
  live = [t for t in trades if str(t.get("mode")).lower() == "live"]
  enters = [t for t in live if t.get("action") == "enter"]
  exits_by_leg: dict[str, dict[str, Any]] = {}
  for t in live:
    if t.get("action") != "exit":
      continue
    key = f"{t.get('market_ticker')}|{t.get('side')}|{t.get('created_at', '')[:16]}"
    exits_by_leg[key] = t

  for ent in enters:
    ticker = str(ent.get("market_ticker") or "")
    strat = _strategy(ticker)
    side = str(ent.get("side") or "").lower()
    detail = str(ent.get("detail") or "")
    label = str(ent.get("label") or ticker)

    # Abnormal S2 YES below band floor at entry (guard should block post 4.0.71+)
    floor = ent.get("floor_strike")
    if strat == "S2" and side == "yes" and floor is not None and spot is not None:
      try:
        gap = float(floor) - float(spot)
        if gap > 50:
          issues.append({
            "severity": "high",
            "code": "s2_yes_below_spot",
            "asset": asset,
            "strat": strat,
            "label": label,
            "gap_usd": round(gap, 0),
            "spot": spot,
            "detail": detail[:120],
            "at": ent.get("created_at"),
          })
      except (TypeError, ValueError):
        pass

    # Tail entry with high ask_edge in detail
    if "ask_edge=" in detail:
      m = re.search(r"ask_edge=(\d+)", detail)
      if m and int(m.group(1)) >= 20 and strat == "S2":
        issues.append({
          "severity": "medium",
          "code": "s2_high_ask_edge_entry",
          "asset": asset,
          "strat": strat,
          "label": label,
          "ask_edge_c": int(m.group(1)),
          "at": ent.get("created_at"),
        })

    # Match exit (same ticker, same hour event)
    evt = ent.get("event_ticker")
    matched_exit = None
    for t in live:
      if t.get("action") != "exit":
        continue
      if t.get("market_ticker") != ent.get("market_ticker"):
        continue
      if t.get("side") != ent.get("side"):
        continue
      if t.get("event_ticker") != evt:
        continue
      et = _parse_ts(ent.get("created_at"))
      xt = _parse_ts(t.get("created_at"))
      if et and xt and xt >= et:
        matched_exit = t
        break

    hold = _hold_seconds(ent, matched_exit)
    if hold is not None:
      reason = str((matched_exit or {}).get("detail") or "")
      if matched_exit and hold < min_hold_s and "leg stop" not in reason.lower():
        issues.append({
          "severity": "medium",
          "code": "exit_before_min_hold",
          "asset": asset,
          "strat": strat,
          "label": label,
          "hold_s": round(hold, 0),
          "min_hold_s": min_hold_s,
          "exit_detail": reason[:100],
          "at": matched_exit.get("created_at"),
        })
      if matched_exit is None and hold > 2700 and ticker in open_tickers:
        issues.append({
          "severity": "medium",
          "code": "open_leg_long_hold",
          "asset": asset,
          "strat": strat,
          "label": label,
          "hold_s": round(hold, 0),
          "at": ent.get("created_at"),
        })
      if matched_exit:
        pnl = float(matched_exit.get("pnl_usd") or 0)
        if "take profit" in reason.lower() and pnl < 0.02:
          issues.append({
            "severity": "low",
            "code": "take_profit_label_low_pnl",
            "asset": asset,
            "strat": strat,
            "label": label,
            "pnl_usd": pnl,
            "at": matched_exit.get("created_at"),
          })
        if "leg stop" in reason.lower() and strat == "S2" and side == "yes":
          issues.append({
            "severity": "info",
            "code": "s2_leg_stop_exit",
            "asset": asset,
            "label": label,
            "pnl_usd": pnl,
            "at": matched_exit.get("created_at"),
          })

  scratch = sum(
    1 for t in live
    if t.get("action") == "exit"
    and float(t.get("pnl_usd") or 0) == 0
    and str(t.get("status") or "") == "reconciled"
  )
  if scratch >= 3:
    issues.append({
      "severity": "high",
      "code": "scratch_reconcile_exits",
      "asset": asset,
      "count": scratch,
      "hint": "run sync-kalshi-fills",
    })
  return issues


def _analyze_compare(compare: dict[str, Any], asset: str) -> list[dict[str, Any]]:
  issues: list[dict[str, Any]] = []
  if not compare.get("ok", True) and compare.get("error"):
    issues.append({"severity": "high", "code": "compare_fetch_error", "asset": asset, "error": compare.get("error")})
    return issues
  for hour in (compare.get("hours") or [])[:3]:
    evt = hour.get("event_ticker")
    live_pnl = float((hour.get("live") or {}).get("net_pnl_usd") or 0)
    trial_pnl = float((hour.get("trial") or {}).get("net_pnl_usd") or 0)
    pairs = hour.get("entry_pairs") or {}
    unpaired_live = pairs.get("unpaired_live") or []
    unpaired_trial = pairs.get("unpaired_trial") or []
    if unpaired_live:
      issues.append({
        "severity": "medium",
        "code": "compare_unpaired_live",
        "asset": asset,
        "event": evt,
        "count": len(unpaired_live),
        "samples": [u.get("market_ticker") for u in unpaired_live[:3]],
      })
    if abs(live_pnl - trial_pnl) > 2.0 and (live_pnl != 0 or trial_pnl != 0):
      issues.append({
        "severity": "medium",
        "code": "compare_pnl_divergence",
        "asset": asset,
        "event": evt,
        "live_pnl": live_pnl,
        "trial_pnl": trial_pnl,
      })
  return issues


def _analyze_reconcile(recon: dict[str, Any], asset: str) -> list[dict[str, Any]]:
  if recon.get("status") == 404 or recon.get("error") == "HTTP 404":
    return [{
      "severity": "medium",
      "code": "reconcile_endpoint_missing",
      "asset": asset,
    }]
  if recon.get("ok"):
    return []
  mism = len(recon.get("mismatches") or [])
  bot_only = len(recon.get("bot_only") or [])
  kalshi_only = len(recon.get("kalshi_only") or [])
  if mism + bot_only + kalshi_only == 0:
    return []
  return [{
    "severity": "high",
    "code": "kalshi_reconcile_mismatch",
    "asset": asset,
    "mismatches": mism,
    "bot_only": bot_only,
    "kalshi_only": kalshi_only,
  }]


def _analyze_open_positions(bundle: dict[str, Any]) -> list[dict[str, Any]]:
  issues: list[dict[str, Any]] = []
  asset = bundle["asset"]
  live = bundle.get("live") or {}
  spot = _index_spot(bundle)
  for pos in live.get("open_positions") or []:
    ticker = str(pos.get("market_ticker") or "")
    strat = _strategy(ticker)
    side = str(pos.get("side") or "").lower()
    floor = pos.get("floor_strike")
    cap = pos.get("cap_strike")
    unreal = pos.get("unrealized_pnl_usd")
    if strat == "S2" and side == "yes" and floor is not None and spot is not None:
      try:
        if float(spot) + 75 < float(floor):
          issues.append({
            "severity": "high",
            "code": "open_s2_yes_spot_below_floor",
            "asset": asset,
            "ticker": ticker,
            "spot": spot,
            "floor": floor,
            "unreal_usd": unreal,
          })
      except (TypeError, ValueError):
        pass
    if unreal is not None:
      try:
        u = float(unreal)
        if u >= 0.25:
          issues.append({
            "severity": "low",
            "code": "open_unrealized_profit",
            "asset": asset,
            "strat": strat,
            "ticker": ticker,
            "unreal_usd": u,
            "hint": "check take-profit / mark",
          })
      except (TypeError, ValueError):
        pass
  return issues


def _maybe_sync_fills(session: requests.Session, base: str, asset: str, issues: list[dict[str, Any]]) -> dict[str, Any] | None:
  codes = {i.get("code") for i in issues}
  if not codes & {"scratch_reconcile_exits", "kalshi_reconcile_mismatch"}:
    return None
  p = _api_prefix(asset)
  r = session.post(f"{base}{p}/hourly/bot/sync-kalshi-fills", timeout=120)
  try:
    return r.json()
  except Exception:
    return {"ok": False, "status": r.status_code, "text": r.text[:200]}


def run_cycle(
  session: requests.Session,
  base: str,
  *,
  auto_sync: bool,
  cycle: int,
  lookback_hours: float = 2.0,
) -> dict[str, Any]:
  ts = datetime.now(timezone.utc).isoformat()
  report: dict[str, Any] = {
    "ts": ts, "cycle": cycle, "assets": {}, "trials": {}, "window": {},
    "issues": [], "actions": [],
  }
  prev_state = _load_last_cycle_state()
  prev_ts = _parse_ts((prev_state or {}).get("ts"))
  prev_assets = (prev_state or {}).get("assets") or {}

  if cycle == 1:
    report["issues"].extend(_validate_production(session, base))

  live_skip_by_asset: dict[str, str | None] = {}
  spot_by_asset: dict[str, float | None] = {}

  for asset in ("btc", "eth"):
    bundle = _fetch_asset_bundle(session, base, asset)
    spot = _index_spot(bundle)
    trades_all = (bundle.get("trades") or {}).get("trades") or []
    trades = _trades_in_lookback(trades_all, lookback_hours)
    live = bundle.get("live") or {}
    settings = live.get("settings") or {}
    open_positions = live.get("open_positions") or []
    open_tickers = {str(p.get("market_ticker") or "") for p in open_positions}

    asset_issues: list[dict[str, Any]] = []
    interval_pnl = (live.get("interval_performance") or {}).get("net_pnl_usd")
    try:
      interval_pnl_f = float(interval_pnl) if interval_pnl is not None else None
    except (TypeError, ValueError):
      interval_pnl_f = None
    live_enters = [
      t for t in trades
      if str(t.get("mode", "")).lower() == "live" and t.get("action") == "enter"
    ]
    asset_issues.extend(_analyze_abnormal_enters(
      live_enters, asset=asset, interval_net_pnl=interval_pnl_f,
    ))
    asset_issues.extend(_analyze_exit_spam(trades, asset=asset))
    asset_issues.extend(_analyze_trades(trades, asset=asset, spot=spot, open_tickers=open_tickers))
    asset_issues.extend(_analyze_compare(bundle.get("compare") or {}, asset))
    asset_issues.extend(_analyze_reconcile(bundle.get("reconcile") or {}, asset))
    asset_issues.extend(_analyze_open_positions(bundle))

    s1_open = sum(1 for p in (live.get("open_positions") or []) if _strategy(p.get("market_ticker")) == "S1")
    s2_open = sum(1 for p in (live.get("open_positions") or []) if _strategy(p.get("market_ticker")) == "S2")

    kfs = bundle.get("kalshi_fill_summary") or {}
    if kfs.get("ok") and int(kfs.get("closed_trades") or 0) == 0 and int(kfs.get("fills_scanned") or 0) > 20:
      asset_issues.append({
        "severity": "high",
        "code": "kalshi_fill_summary_zero_closed",
        "asset": asset,
        "fills_scanned": kfs.get("fills_scanned"),
      })

    report["assets"][asset] = {
      "mode": settings.get("mode"),
      "skip": live.get("last_skip_reason"),
      "spot": spot,
      "open_s1": s1_open,
      "open_s2": s2_open,
      "open_exposure_usd": live.get("open_exposure_live_usd", live.get("open_exposure_usd")),
      "interval_net_pnl": (live.get("interval_performance") or {}).get("net_pnl_usd"),
      "version": live.get("version") or live.get("app_version"),
    }

    window_trades = _trades_since(trades_all, prev_ts)
    win_issues, win_summary = _analyze_window_delta(
      asset=asset,
      window_trades=window_trades,
      prev_snap=prev_assets.get(asset),
      cur_snap=report["assets"][asset],
      interval_net_pnl=interval_pnl_f,
    )
    report["window"][asset] = win_summary
    asset_issues.extend(win_issues)
    report["assets"][asset]["issue_count"] = len(asset_issues)

    live_skip_by_asset[asset] = live.get("last_skip_reason")
    spot_by_asset[asset] = spot
    report["issues"].extend(asset_issues)

    if auto_sync and asset_issues:
      sync_result = _maybe_sync_fills(session, base, asset, asset_issues)
      if sync_result is not None:
        report["actions"].append({"asset": asset, "action": "sync-kalshi-fills", "result": sync_result})

  for asset, variant, bot_path in TRIAL_BOTS:
    bundle = _fetch_trial_bot(session, base, bot_path)
    status = bundle.get("status") or {}
    trades_all = (bundle.get("trades") or {}).get("trades") or []
    trades = _trades_in_lookback(trades_all, lookback_hours)
    snap = _trial_snapshot(
      asset=asset,
      variant=variant,
      status=status,
      trades=trades_all,
      lookback_hours=lookback_hours,
    )
    key = f"{asset}:{variant}"
    trial_issues: list[dict[str, Any]] = []
    trial_issues.extend(
      _analyze_paper_trades(trades, asset=asset, variant=variant, spot=spot_by_asset.get(asset))
    )
    trial_issues.extend(
      _trial_watch_issues(
        asset=asset,
        live_skip=live_skip_by_asset.get(asset),
        trial_snap=snap,
      )
    )
    snap["issue_count"] = len(trial_issues)
    report["trials"][key] = snap
    report["issues"].extend(trial_issues)

  _save_last_cycle_state(report)
  return report


def _print_report(report: dict[str, Any]) -> None:
  ts = report.get("ts", "")[:19]
  print(f"\n{'=' * 72}\nMANAGER CYCLE {report.get('cycle')} @ {ts}Z")
  for asset, snap in (report.get("assets") or {}).items():
    print(
      f"  {asset.upper()} LIVE {snap.get('mode')} v{snap.get('version')} | "
      f"spot={snap.get('spot')} | S1 open={snap.get('open_s1')} S2 open={snap.get('open_s2')} | "
      f"exposure=${snap.get('open_exposure_usd')} | interval PnL=${snap.get('interval_net_pnl')} | "
      f"skip={snap.get('skip') or '—'}"
    )
  window = report.get("window") or {}
  if window:
    print("  — last interval —")
    for asset, w in window.items():
      if not w.get("enters") and not w.get("exits") and not w.get("skip_changed"):
        continue
      print(
        f"    {asset.upper()}: +{w.get('enters', 0)} enter / {w.get('exits', 0)} exit | "
        f"Δexp=${w.get('exposure_delta_usd', '—')} | window PnL=${w.get('window_pnl_usd', 0)}"
      )
  trials = report.get("trials") or {}
  if trials:
    print("  — trial paper —")
    for key, snap in sorted(trials.items()):
      on = "ON" if snap.get("enabled") else "OFF"
      print(
        f"    {key} auto={on} | S1={snap.get('open_s1')} S2={snap.get('open_s2')} | "
        f"${snap.get('remaining_usd')} deploy | interval PnL=${snap.get('interval_net_pnl')} | "
        f"2h: {snap.get('lookback_enters')} enters / ${snap.get('lookback_net_pnl')} | "
        f"skip={snap.get('skip') or '—'}"
      )
  issues = report.get("issues") or []
  if not issues:
    print("  ✓ No issues flagged this cycle")
  else:
    by_sev: dict[str, list] = {"high": [], "medium": [], "low": [], "info": []}
    for i in issues:
      by_sev.setdefault(str(i.get("severity", "info")), []).append(i)
    for sev in ("high", "medium", "low", "info"):
      for i in by_sev.get(sev, []):
        print(f"  [{sev.upper()}] {i.get('code')} ({i.get('asset', '?')}) {json.dumps({k: v for k, v in i.items() if k not in ('severity', 'code', 'asset')}, default=str)[:200]}")
  for act in report.get("actions") or []:
    print(f"  ACTION: {act.get('asset')} {act.get('action')} -> {str(act.get('result'))[:120]}")


def main() -> int:
  parser = argparse.ArgumentParser(description="2h hourly S1/S2 manager monitor")
  parser.add_argument("--base", default=DEFAULT_BASE)
  parser.add_argument("--duration-minutes", type=float, default=120.0)
  parser.add_argument("--interval", type=float, default=30.0, help="Seconds between cycles (default 30s)")
  parser.add_argument("--no-auto-sync", action="store_true")
  parser.add_argument("--lookback-hours", type=float, default=2.0)
  parser.add_argument("--once", action="store_true")
  args = parser.parse_args()

  pw = _password()
  if not pw:
    print("Set APP_PASSWORD in .env", file=sys.stderr)
    return 1

  LOG_DIR.mkdir(parents=True, exist_ok=True)
  log_path = LOG_DIR / f"monitor_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.jsonl"

  session = requests.Session()
  _login(session, args.base, pw)

  deadline = time.time() + args.duration_minutes * 60
  cycle = 0
  print(f"Manager monitor started — log: {log_path}", flush=True)
  print(f"Duration: {args.duration_minutes}m | interval: {args.interval}s | auto-sync: {not args.no_auto_sync}", flush=True)

  while True:
    cycle += 1
    try:
      report = run_cycle(
        session, args.base,
        auto_sync=not args.no_auto_sync,
        cycle=cycle,
        lookback_hours=args.lookback_hours,
      )
      _print_report(report)
      with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(report, default=str) + "\n")
    except Exception as e:
      err = {"ts": datetime.now(timezone.utc).isoformat(), "cycle": cycle, "error": str(e)}
      print(f"ERROR cycle {cycle}: {e}", file=sys.stderr)
      with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(err) + "\n")

    if args.once:
      break
    if time.time() >= deadline:
      print(f"\nMonitor complete after {cycle} cycles. Log: {log_path}", flush=True)
      break
    time.sleep(max(5.0, args.interval))

  return 0


if __name__ == "__main__":
  raise SystemExit(main())
