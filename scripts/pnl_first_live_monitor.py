#!/usr/bin/env python3
"""P&L-first POA live monitor — BTC hourly S1-only, sleep locks, fill/reconcile anomalies.

Each cycle diffs against the prior poll (default 30s). Logs issues + state under
data/logs/pnl_first_live_monitor/.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
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
LOG_DIR = ROOT / "data" / "logs" / "pnl_first_live_monitor"
STATE_PATH = LOG_DIR / "last_cycle.json"
ISSUES_LOG = LOG_DIR / "issues.jsonl"
CYCLE_LOG = LOG_DIR / "cycles.jsonl"
STATUS_PATH = LOG_DIR / "monitor_status.json"

POA_WAKE_UTC = "2026-07-05T19:01:13+00:00"
S1_RE = re.compile(r"-T\d", re.I)
S2_RE = re.compile(r"-B\d", re.I)
MAX_LIVE_CONTRACTS = 8
TAIL_MAX_CENTS = 20


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
    raise RuntimeError(f"login failed: {r.status_code}")


def _get(session: requests.Session, url: str, **kw) -> dict[str, Any]:
  r = session.get(url, timeout=kw.get("timeout", 45), params=kw.get("params"))
  try:
    body = r.json()
  except Exception:
    body = {"ok": False, "error": r.text[:300]}
  if r.status_code >= 400 and "error" not in body:
    body = {"ok": False, "error": f"HTTP {r.status_code}"}
  return body


def _parse_ts(raw: str | None) -> datetime | None:
  if not raw:
    return None
  try:
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
  except ValueError:
    return None


def _strategy(ticker: str | None) -> str:
  t = str(ticker or "")
  if S2_RE.search(t):
    return "S2"
  if S1_RE.search(t):
    return "S1"
  return "?"


def _edge_cents(raw: Any) -> float | None:
  try:
    v = float(raw)
  except (TypeError, ValueError):
    return None
  return round(v * 100.0, 1)


def _poa_session_pnl(trade_rows: list[dict[str, Any]]) -> dict[str, Any]:
  """Rolling POA-session stats since POA_WAKE_UTC (live exits + open exposure)."""
  poa_dt = _parse_ts(POA_WAKE_UTC)
  realized = 0.0
  wins = losses = enters = 0
  edges: list[float] = []
  for t in trade_rows:
    if str(t.get("mode")).lower() != "live":
      continue
    ts = _parse_ts(t.get("created_at"))
    if poa_dt and ts and ts < poa_dt:
      continue
    action = t.get("action")
    if action == "enter" and str(t.get("status") or "").lower() in ("filled", "reconciled"):
      enters += 1
      ec = _edge_cents(t.get("entry_edge"))
      if ec is None:
        m = re.search(r"ask_edge=(\d+)", str(t.get("detail") or ""))
        if m:
          ec = float(m.group(1))
      if ec is not None:
        edges.append(ec)
    if action == "exit" and t.get("pnl_usd") is not None:
      pnl = float(t.get("pnl_usd"))
      realized += pnl
      if pnl > 0.005:
        wins += 1
      elif pnl < -0.005:
        losses += 1
      ctx = t.get("exit_context") or {}
      ec = _edge_cents(ctx.get("entry_edge"))
      if ec is not None:
        edges.append(ec)
  closed = wins + losses
  return {
    "realized_usd": round(realized, 2),
    "wins": wins,
    "losses": losses,
    "win_rate_pct": round(100.0 * wins / closed, 1) if closed else None,
    "enters": enters,
    "avg_entry_edge_cents": round(sum(edges) / len(edges), 1) if edges else None,
  }


def _snap_btc(session: requests.Session, base: str) -> dict[str, Any]:
  health = _get(session, f"{base}/health")
  btc = _get(session, f"{base}/api/hourly/bot")
  mgr = _get(session, f"{base}/api/pnl-first/manager")
  eth = _get(session, f"{base}/api/eth/hourly/bot")
  btc15 = _get(session, f"{base}/api/slot15/bot")
  eth15 = _get(session, f"{base}/api/eth/15m/bot")
  recon = _get(session, f"{base}/api/hourly/bot/live-reconcile")
  trades = _get(session, f"{base}/api/hourly/bot/trades", params={"limit": 80})
  event = btc.get("event_ticker")
  fills = _get(session, f"{base}/api/hourly/bot/kalshi-fill-summary", params={"since": POA_WAKE_UTC})
  kalshi_hour: dict[str, Any] = {}
  if event:
    kalshi_hour = _get(
      session,
      f"{base}/api/hourly/bot/kalshi-fill-summary",
      params={"since": POA_WAKE_UTC, "event_ticker": str(event)},
    )
  tab = _get(session, f"{base}/api/daily/prediction", timeout=90)

  st = btc.get("settings") or btc
  guards = btc.get("live_entry_guards") or {}
  trade_rows = trades if isinstance(trades, list) else trades.get("trades") or []
  hs = btc.get("hour_summary") or {}
  ip = btc.get("interval_performance") or {}
  session_pnl = _poa_session_pnl(trade_rows)
  poa_dt = _parse_ts(POA_WAKE_UTC)
  poa_enters = []
  for t in trade_rows:
    if str(t.get("mode")).lower() != "live" or t.get("action") != "enter":
      continue
    ts = _parse_ts(t.get("created_at"))
    if poa_dt and ts and ts >= poa_dt:
      poa_enters.append(t)

  live = tab.get("live") or {}
  regime = live.get("regime") or {}
  return {
    "ts": datetime.now(timezone.utc).isoformat(),
    "version": health.get("version"),
    "balance_usd": (health.get("kalshi") or {}).get("balance_usd"),
    "btc": {
      "enabled": st.get("enabled"),
      "mode": st.get("mode"),
      "cap": st.get("max_spend_per_hour_usd"),
      "skip": btc.get("last_skip_reason"),
      "event": btc.get("event_ticker"),
      "open_legs": len(btc.get("open_positions") or []),
      "exposure": btc.get("open_exposure_live_usd", 0),
      "profile": guards.get("mechanics_profile"),
      "hour_enters": (btc.get("hour_summary") or {}).get("filled_enter_count_this_hour"),
    },
    "locks": {
      "eth_hourly": {"enabled": (eth.get("settings") or eth).get("enabled"), "mode": (eth.get("settings") or eth).get("mode")},
      "btc_15m": {"enabled": (btc15.get("settings") or btc15).get("enabled"), "mode": (btc15.get("settings") or btc15).get("mode")},
      "eth_15m": {"enabled": (eth15.get("settings") or eth15).get("enabled"), "mode": (eth15.get("settings") or eth15).get("mode")},
    },
    "manager": {
      "preflight_ok": (mgr.get("preflight_now") or {}).get("ok"),
      "poa_live": ((mgr.get("runtime") or {}).get("persisted") or {}).get("poa_live_active"),
      "milestone": mgr.get("milestone_now"),
    },
    "reconcile": {
      "ok": recon.get("ok"),
      "kalshi_only": len(recon.get("kalshi_only") or []),
      "bot_only": len(recon.get("bot_only") or []),
      "mismatches": len(recon.get("mismatches") or []),
    },
    "regime": regime,
    "poa_session_enters": len(poa_enters),
    "pnl": {
      "hour_realized_usd": hs.get("realized_pnl_usd"),
      "hour_total_usd": hs.get("total_pnl_usd"),
      "hour_unrealized_usd": hs.get("unrealized_pnl_usd"),
      "interval_net_usd": ip.get("net_interval_pnl_usd"),
      "interval_win_rate_pct": ip.get("win_rate_pct"),
      "session": session_pnl,
      "kalshi_poa_pnl_usd": fills.get("total_pnl_usd"),
      "kalshi_poa_closed_legs": fills.get("closed_trades"),
      "kalshi_poa_win_rate": fills.get("win_rate"),
      "kalshi_hour_pnl_usd": kalshi_hour.get("total_pnl_usd"),
      "kalshi_hour_closed": kalshi_hour.get("closed_trades"),
      "hour_partial": bool(btc.get("open_positions")) or int(hs.get("resting_exit_count") or 0) > 0,
    },
    "trade_ids": [t.get("id") for t in trade_rows[:40]],
  }


def _analyze(snap: dict[str, Any], prev: dict[str, Any] | None, window_trades: list[dict]) -> list[dict[str, Any]]:
  issues: list[dict[str, Any]] = []
  btc = snap.get("btc") or {}
  locks = snap.get("locks") or {}

  if btc.get("mode") != "live" or not btc.get("enabled"):
    issues.append({"severity": "high", "code": "btc_not_live", "detail": btc})
  if btc.get("profile") != "pnl_first":
    issues.append({"severity": "high", "code": "btc_not_pnl_first", "profile": btc.get("profile")})

  for name, row in locks.items():
    if row.get("enabled") and str(row.get("mode")).lower() == "live":
      issues.append({"severity": "high", "code": "sleep_lock_breach", "bot": name, "state": row})

  if not (snap.get("manager") or {}).get("preflight_ok"):
    issues.append({"severity": "high", "code": "preflight_failed"})

  recon = snap.get("reconcile") or {}
  if recon.get("kalshi_only") or recon.get("bot_only") or recon.get("mismatches"):
    issues.append({"severity": "medium", "code": "reconcile_drift", "detail": recon})

  pnl = snap.get("pnl") or {}
  if pnl.get("hour_partial"):
    issues.append({"severity": "info", "code": "hour_pnl_partial", "detail": "open legs or resting exits — use Kalshi history for final hour P&L"})
  bot_hr = pnl.get("hour_realized_usd")
  kalshi_hr = pnl.get("kalshi_hour_pnl_usd")
  if (
    not pnl.get("hour_partial")
    and bot_hr is not None
    and kalshi_hr is not None
    and abs(float(bot_hr) - float(kalshi_hr)) > 0.12
  ):
    issues.append({
      "severity": "medium",
      "code": "kalshi_hour_pnl_drift",
      "bot_usd": bot_hr,
      "kalshi_usd": kalshi_hr,
      "delta_usd": round(float(bot_hr) - float(kalshi_hr), 2),
    })

  skip = str(btc.get("skip") or "")
  if "missing_price:" in skip:
    issues.append({"severity": "high", "code": "execution_missing_price", "skip": skip})
  if "pnl_first_taker" in skip or "blocked_taker" in skip:
    issues.append({"severity": "high", "code": "taker_execution_blocked", "skip": skip})
  if "pnl_first_s2_blocked" not in skip and _strategy(skip.split(":")[-1] if ":" in skip else "") == "S2":
    pass
  if prev and prev.get("btc", {}).get("skip") != skip:
    issues.append({"severity": "info", "code": "skip_changed", "from": prev.get("btc", {}).get("skip"), "to": skip})

  poa_dt = _parse_ts(POA_WAKE_UTC)
  prev_ids = set(prev.get("trade_ids") or []) if prev else set()
  for t in window_trades:
    tid = t.get("id")
    if tid in prev_ids:
      continue
    if str(t.get("mode")).lower() != "live":
      continue
    ts = _parse_ts(t.get("created_at"))
    if poa_dt and ts and ts < poa_dt:
      continue
    action = t.get("action")
    ticker = str(t.get("market_ticker") or "")
    strat = _strategy(ticker)
    contracts = int(t.get("contracts") or 0)
    price = int(t.get("entry_price_cents") or t.get("price_cents") or 0)
    row = {
      "severity": "info",
      "code": f"live_{action}",
      "ticker": ticker,
      "strategy": strat,
      "contracts": contracts,
      "price_cents": price,
      "detail": (t.get("detail") or "")[:120],
      "at": t.get("created_at"),
    }
    if action == "enter":
      if strat == "S2":
        row["severity"] = "high"
        row["code"] = "abnormal_s2_live_enter"
      if contracts > MAX_LIVE_CONTRACTS:
        row["severity"] = "high"
        row["code"] = "abnormal_oversized_enter"
      if 1 <= price <= TAIL_MAX_CENTS:
        row["severity"] = "medium"
        row["code"] = "tail_entry_on_pnl_first"
    if action == "exit" and t.get("pnl_usd") is not None and float(t.get("pnl_usd")) < -1.0:
      row["severity"] = "medium"
      row["code"] = "live_loss_exit"
    issues.append(row)

  return issues


def _print_cycle(cycle: int, snap: dict[str, Any], issues: list[dict[str, Any]]) -> None:
  btc = snap.get("btc") or {}
  mile = (snap.get("manager") or {}).get("milestone") or {}
  pnl = snap.get("pnl") or {}
  sess = pnl.get("session") or {}
  hi = [i for i in issues if i.get("severity") == "high"]
  med = [i for i in issues if i.get("severity") == "medium"]
  ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
  hr_pnl = pnl.get("hour_total_usd")
  sess_pnl = sess.get("realized_usd")
  kalshi_poa = pnl.get("kalshi_poa_pnl_usd")
  kalshi_hr = pnl.get("kalshi_hour_pnl_usd")
  partial = pnl.get("hour_partial")
  wr = sess.get("win_rate_pct")
  edge = sess.get("avg_entry_edge_cents")
  partial_tag = " (hour partial)" if partial else ""
  print(
    f"[{ts}] cycle={cycle} v={snap.get('version')} "
    f"BTC live skip={str(btc.get('skip') or '')[:50]} "
    f"legs={btc.get('open_legs')} "
    f"hour_pnl=${hr_pnl}{partial_tag} kalshi_hr=${kalshi_hr} sess_pnl=${sess_pnl} kalshi_poa=${kalshi_poa} "
    f"W/L={sess.get('wins')}/{sess.get('losses')} "
    f"avg_edge={edge}c milestone={mile.get('consecutive_pipeline_hours')}/{mile.get('target_pipeline_hours')} "
    f"HIGH={len(hi)} MED={len(med)}",
    flush=True,
  )
  for issue in hi + med:
    print(f"  ! {issue.get('severity').upper()} {issue.get('code')}: {json.dumps(issue, default=str)[:200]}", flush=True)


def run_cycle(session: requests.Session, base: str, cycle: int, prev: dict[str, Any] | None) -> dict[str, Any]:
  if cycle > 1 and cycle % 15 == 0:
    try:
      session.post(f"{base}/api/hourly/bot/sync-kalshi-fills", timeout=120)
    except Exception:
      pass
  snap = _snap_btc(session, base)
  trades = _get(session, f"{base}/api/hourly/bot/trades", params={"limit": 80})
  trade_rows = trades if isinstance(trades, list) else trades.get("trades") or []
  issues = _analyze(snap, prev, trade_rows)
  _print_cycle(cycle, snap, issues)

  LOG_DIR.mkdir(parents=True, exist_ok=True)
  with CYCLE_LOG.open("a", encoding="utf-8") as f:
    f.write(json.dumps({"cycle": cycle, "snap": snap, "issues": issues}, default=str) + "\n")
  for issue in issues:
    if issue.get("severity") in ("high", "medium"):
      with ISSUES_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"cycle": cycle, "ts": snap.get("ts"), **issue}, default=str) + "\n")

  STATE_PATH.write_text(json.dumps(snap, indent=2, default=str), encoding="utf-8")
  STATUS_PATH.write_text(
    json.dumps(
      {
        "state": "running",
        "cycle": cycle,
        "updated_at": snap.get("ts"),
        "last_high_count": sum(1 for i in issues if i.get("severity") == "high"),
        "version": snap.get("version"),
      },
      indent=2,
    ),
    encoding="utf-8",
  )
  return snap


def main() -> int:
  parser = argparse.ArgumentParser(description="P&L-first POA live monitor")
  parser.add_argument("--base", default=DEFAULT_BASE)
  parser.add_argument("--interval", type=float, default=30.0)
  parser.add_argument("--duration-minutes", type=float, default=0.0, help="0 = run until interrupted")
  parser.add_argument("--once", action="store_true")
  args = parser.parse_args()

  password = _password()
  session = requests.Session()
  _login(session, args.base, password)

  LOG_DIR.mkdir(parents=True, exist_ok=True)
  prev = None
  if STATE_PATH.exists():
    try:
      prev = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
      prev = None

  started = time.time()
  deadline = started + args.duration_minutes * 60 if args.duration_minutes > 0 else None
  cycle = 0

  STATUS_PATH.write_text(
    json.dumps({"state": "starting", "started_at": datetime.now(timezone.utc).isoformat()}, indent=2),
    encoding="utf-8",
  )
  print(f"pnl_first_live_monitor ON interval={args.interval}s base={args.base}", flush=True)

  try:
    while True:
      cycle += 1
      prev = run_cycle(session, args.base, cycle, prev)
      if args.once:
        break
      if deadline is not None and time.time() >= deadline:
        break
      time.sleep(args.interval)
  except KeyboardInterrupt:
    print("monitor stopped (interrupt)", flush=True)
  finally:
    STATUS_PATH.write_text(
      json.dumps(
        {"state": "stopped", "cycles": cycle, "stopped_at": datetime.now(timezone.utc).isoformat()},
        indent=2,
      ),
      encoding="utf-8",
    )
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
