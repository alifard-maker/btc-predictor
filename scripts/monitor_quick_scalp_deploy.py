#!/usr/bin/env python3
"""Monitor production for quick-scalp defense exits after 4.0.38 deploy."""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

BASE = os.environ.get(
  "BTC_PREDICTOR_URL",
  "https://btc-predictor-production-f460.up.railway.app",
)
POLL_SEC = float(os.environ.get("MONITOR_POLL_SEC", "180"))  # 3 min
MAX_MINUTES = float(os.environ.get("MONITOR_MAX_MIN", "20"))
TARGET_VERSION = os.environ.get("MONITOR_TARGET_VERSION", "4.0.38")
MIN_PROFIT_USD = float(os.environ.get("MONITOR_MIN_PROFIT_USD", "0.06"))
MIN_HOLD = float(os.environ.get("MONITOR_MIN_HOLD_SEC", "30"))
MAX_HOLD = float(os.environ.get("MONITOR_MAX_HOLD_SEC", "90"))


def _password() -> str:
  env_path = Path(__file__).resolve().parents[1] / ".env"
  if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
      if line.startswith("APP_PASSWORD="):
        return line.split("=", 1)[1].strip()
  return os.environ.get("APP_PASSWORD", "")


def _login(session: requests.Session) -> bool:
  pw = _password()
  if not pw:
    return False
  r = session.post(f"{BASE}/api/auth/login", data={"password": pw}, allow_redirects=False, timeout=30)
  return r.status_code in (200, 302, 303)


def _health(session: requests.Session) -> dict[str, Any]:
  return session.get(f"{BASE}/health", timeout=30).json()


def _open_positions(session: requests.Session) -> dict[str, list[dict]]:
  out: dict[str, list[dict]] = {}
  for key, path in (
    ("btc", "/api/hourly/bot"),
    ("eth", "/api/eth/hourly/bot"),
  ):
    resp = session.get(f"{BASE}{path}", timeout=30)
    if resp.ok:
      bot = resp.json()
      out[key] = bot.get("open_positions") or []
  return out


def _trade_sources(session: requests.Session) -> list[tuple[str, list[dict]]]:
  out: list[tuple[str, list[dict]]] = []
  for label, path in (
    ("btc_hourly", "/api/hourly/bot/trades?limit=200"),
    ("eth_hourly", "/api/eth/hourly/bot/trades?limit=200"),
  ):
    resp = session.get(f"{BASE}{path}", timeout=60)
    resp.raise_for_status()
    trades = resp.json().get("trades") or []
    out.append((label, [t for t in trades if t.get("mode") == "live"]))
  return out


def _is_defense_entry(enter: dict | None) -> bool:
  if not enter:
    return False
  es = enter.get("entry_settings") or {}
  adaptive = es.get("adaptive") or {}
  mode = str(adaptive.get("entry_mode") or "").lower()
  if mode == "defense":
    return True
  hm = es.get("hour_momentum") or {}
  return str(hm.get("state") or "").lower() in ("conservative", "defense")


def _enter_by_position(trades: list[dict]) -> dict[str, dict]:
  m: dict[str, dict] = {}
  for t in trades:
    if t.get("action") == "enter":
      pid = t.get("position_id")
      if pid:
        m[pid] = t
  return m


def _profit_usd(exit_trade: dict) -> float | None:
  if exit_trade.get("realized_pnl_usd") is not None:
    return float(exit_trade["realized_pnl_usd"])
  if exit_trade.get("pnl_usd") is not None:
    return float(exit_trade["pnl_usd"])
  return None


def _matches_success(exit_trade: dict, enter: dict | None) -> tuple[bool, str]:
  status = exit_trade.get("status")
  detail = (exit_trade.get("detail") or "").lower()
  if status != "filled":
    return False, f"status={status}"
  if "unverified" in detail and "inventory unchanged" in detail:
    return False, "unverified exit"
  if "backfilled" in detail:
    return False, "backfilled"

  ctx = exit_trade.get("exit_context") or {}
  hold = ctx.get("hold_seconds")
  if hold is None and enter:
    t0 = enter.get("created_at")
    t1 = exit_trade.get("created_at")
    if t0 and t1:
      try:
        d0 = datetime.fromisoformat(t0.replace("Z", "+00:00"))
        d1 = datetime.fromisoformat(t1.replace("Z", "+00:00"))
        hold = (d1 - d0).total_seconds()
      except ValueError:
        hold = None

  if hold is None:
    return False, "no hold_seconds"
  if not (MIN_HOLD <= float(hold) <= MAX_HOLD):
    return False, f"hold={float(hold):.0f}s outside {MIN_HOLD:.0f}-{MAX_HOLD:.0f}s"

  if not ctx.get("quick_exit_applied"):
    return False, "quick_exit_applied=false"

  if not _is_defense_entry(enter):
    return False, "not defense/conservative entry"

  pnl = _profit_usd(exit_trade)
  if pnl is None or pnl < MIN_PROFIT_USD:
    return False, f"pnl=${pnl} < ${MIN_PROFIT_USD:.2f}"

  return True, (
    f"hold={float(hold):.0f}s pnl=${pnl:.2f} quick_exit=true defense filled"
  )


def _recent_exits(trades: list[dict], limit: int = 5) -> list[dict]:
  exits = [t for t in trades if t.get("action") == "exit"]
  exits.sort(key=lambda t: t.get("created_at") or "", reverse=True)
  return exits[:limit]


def _fmt_exit(source: str, t: dict, enter: dict | None) -> str:
  ctx = t.get("exit_context") or {}
  hold = ctx.get("hold_seconds")
  qx = ctx.get("quick_exit_applied")
  pnl = _profit_usd(t)
  defense = _is_defense_entry(enter)
  ca = (t.get("created_at") or "")[:19]
  return (
    f"  {ca} | {source} | {t.get('status')} | hold={hold} | qx={qx} | "
    f"pnl=${pnl} | defense={defense} | {(t.get('detail') or '')[:80]}"
  )


def main() -> int:
  session = requests.Session()
  has_auth = _login(session)
  if not has_auth:
    print("WARN: no APP_PASSWORD — health-only mode", flush=True)

  seen_ids: set[str] = set()
  if has_auth:
    for _, trades in _trade_sources(session):
      for t in trades:
        if t.get("id"):
          seen_ids.add(t["id"])

  started = datetime.now(timezone.utc)
  deadline = time.time() + MAX_MINUTES * 60
  poll = 0
  success_hit: list[str] = []
  poll_summaries: list[str] = []
  last_version = "?"

  while time.time() <= deadline:
    poll += 1
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    try:
      health = _health(session)
      last_version = str(health.get("version") or "?")
      lh = ((health.get("bot_risk") or {}).get("live_hourly") or {})
      btc_state = lh.get("btc") or {}
      eth_state = lh.get("eth") or {}
      btc_mom = (btc_state.get("hour_momentum") or {}).get("state", "?")
      eth_mom = (eth_state.get("hour_momentum") or {}).get("state", "?")

      open_btc = open_eth = 0
      recent_lines: list[str] = []
      if has_auth:
        positions = _open_positions(session)
        open_btc = len(positions.get("btc") or [])
        open_eth = len(positions.get("eth") or [])
        for source, trades in _trade_sources(session):
          enters = _enter_by_position(trades)
          for ex in _recent_exits(trades, limit=3):
            enter = enters.get(ex.get("position_id") or "")
            recent_lines.append(_fmt_exit(source, ex, enter))
          for t in trades:
            tid = t.get("id")
            if not tid or tid in seen_ids:
              continue
            seen_ids.add(tid)
            if t.get("action") != "exit":
              continue
            enter = enters.get(t.get("position_id") or "")
            ok, reason = _matches_success(t, enter)
            if ok:
              success_hit.append(f"{ts} | NEW SUCCESS | {source} | {reason} | {t.get('label')}")

      summary = (
        f"poll {poll} {ts} | version={last_version} | "
        f"open btc={open_btc} eth={open_eth} | "
        f"btc_mom={btc_mom} eth_mom={eth_mom}"
      )
      poll_summaries.append(summary)
      print(summary, flush=True)
      if recent_lines:
        print("  recent exits:", flush=True)
        for line in recent_lines:
          print(line, flush=True)
      if success_hit:
        print("SUCCESS CRITERIA MET:", flush=True)
        for line in success_hit:
          print(f"  {line}", flush=True)
        break
    except Exception as e:
      err = f"poll {poll} error: {e}"
      poll_summaries.append(err)
      print(err, flush=True)

    if success_hit:
      break
    if poll >= 1 and time.time() + POLL_SEC > deadline:
      break
    time.sleep(POLL_SEC)

  print("\n" + "=" * 72, flush=True)
  print("QUICK SCALP DEPLOY MONITOR REPORT", flush=True)
  print(f"Window: {started.strftime('%H:%M:%S')} – {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC", flush=True)
  print(f"Target version: Beta {TARGET_VERSION} | Observed: {last_version}", flush=True)
  print(f"Polls: {poll} @ {POLL_SEC/60:.1f}min | Auth: {has_auth}", flush=True)
  print(f"Success criteria: defense exit, {MIN_HOLD:.0f}-{MAX_HOLD:.0f}s hold, +${MIN_PROFIT_USD:.2f}+, filled, quick_exit_applied", flush=True)
  print("=" * 72, flush=True)
  if success_hit:
    print("RESULT: SUCCESS", flush=True)
    for line in success_hit:
      print(f"  {line}", flush=True)
  else:
    print("RESULT: criteria not met within window", flush=True)
  print("\nPoll log:", flush=True)
  for s in poll_summaries:
    print(f"  {s}", flush=True)
  return 0 if success_hit else 1


if __name__ == "__main__":
  raise SystemExit(main())
