#!/usr/bin/env python3
"""Extended live monitor: rule audit snapshot, quick-scalp validation, contract mismatch."""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

BASE = os.environ.get(
  "BTC_PREDICTOR_URL",
  "https://btc-predictor-production-f460.up.railway.app",
)
POLL_SEC = float(os.environ.get("MONITOR_POLL_SEC", "180"))
MAX_MINUTES = float(os.environ.get("MONITOR_MAX_MIN", "40"))
TARGET_VERSION = os.environ.get("MONITOR_TARGET_VERSION", "4.0.38")
MIN_PROFIT_USD = float(os.environ.get("MONITOR_MIN_PROFIT_USD", "0.06"))
MIN_HOLD = float(os.environ.get("MONITOR_MIN_HOLD_SEC", "30"))
MAX_HOLD = float(os.environ.get("MONITOR_MAX_HOLD_SEC", "120"))


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


def _asset_bundle(session: requests.Session, asset: str) -> dict[str, Any]:
  prefix = "/api/hourly" if asset == "btc" else f"/api/{asset}/hourly"
  bot = session.get(f"{BASE}{prefix}/bot", timeout=30).json()
  recon_path = f"{prefix}/bot/live-reconcile"
  recon: dict[str, Any] = {}
  if asset == "btc":
    rr = session.get(f"{BASE}{recon_path}", timeout=30)
    if rr.ok:
      recon = rr.json()
  else:
    recon = bot.get("live_reconcile") or {}
    if not recon:
      rr = session.get(f"{BASE}{recon_path}", timeout=30)
      if rr.ok:
        recon = rr.json()
  trades = session.get(f"{BASE}{prefix}/bot/trades?limit=200", timeout=60).json().get("trades") or []
  return {"bot": bot, "reconcile": recon, "trades": [t for t in trades if t.get("mode") == "live"]}


def _is_defense_entry(enter: dict | None) -> bool:
  if not enter:
    return False
  es = enter.get("entry_settings") or {}
  adaptive = es.get("adaptive") or {}
  if str(adaptive.get("entry_mode") or "").lower() == "defense":
    return True
  hm = es.get("hour_momentum") or {}
  return str(hm.get("state") or "").lower() in ("conservative", "defense")


def _enter_by_position(trades: list[dict]) -> dict[str, dict]:
  return {
    t["position_id"]: t
    for t in trades
    if t.get("action") == "enter" and t.get("position_id")
  }


def _profit_usd(exit_trade: dict) -> float | None:
  for key in ("realized_pnl_usd", "pnl_usd"):
    if exit_trade.get(key) is not None:
      return float(exit_trade[key])
  return None


def _matches_scalp(exit_trade: dict, enter: dict | None) -> tuple[bool, str]:
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
    t0, t1 = enter.get("created_at"), exit_trade.get("created_at")
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
  return True, f"hold={float(hold):.0f}s pnl=${pnl:.2f} qx=true defense filled"


@dataclass
class MismatchIncident:
  ts: str
  asset: str
  ticker: str
  side: str
  bot_contracts: float
  kalshi_contracts: float
  delta: float
  kind: str


@dataclass
class MonitorState:
  poll_summaries: list[str] = field(default_factory=list)
  exit_log: list[str] = field(default_factory=list)
  scalp_success: list[str] = field(default_factory=list)
  mismatches: list[MismatchIncident] = field(default_factory=list)
  last_version: str = "?"


def _check_mismatches(ts: str, asset: str, recon: dict[str, Any]) -> list[MismatchIncident]:
  out: list[MismatchIncident] = []
  for row in recon.get("mismatches") or []:
    bot_ct = float(row.get("contracts") or 0)
    kalshi_ct = float(row.get("kalshi_contracts") or 0)
    if kalshi_ct > bot_ct + 0.24:
      out.append(MismatchIncident(
        ts=ts,
        asset=asset,
        ticker=str(row.get("ticker") or "?"),
        side=str(row.get("side") or "?"),
        bot_contracts=bot_ct,
        kalshi_contracts=kalshi_ct,
        delta=round(kalshi_ct - bot_ct, 2),
        kind="count_mismatch",
      ))
  for row in recon.get("kalshi_only") or []:
    out.append(MismatchIncident(
      ts=ts,
      asset=asset,
      ticker=str(row.get("ticker") or "?"),
      side=str(row.get("side") or "?"),
      bot_contracts=0.0,
      kalshi_contracts=float(row.get("contracts") or 0),
      delta=float(row.get("contracts") or 0),
      kind="kalshi_only",
    ))
  return out


def _log_exit(source: str, t: dict, enter: dict | None) -> str:
  ctx = t.get("exit_context") or {}
  bot_ct = t.get("contracts")
  kalshi_note = ""
  detail = t.get("detail") or ""
  if "×" in detail:
    kalshi_note = detail.split("×", 1)[1].split("@", 1)[0].strip()
  return (
    f"{(t.get('created_at') or '')[:19]} | {source} | {t.get('status')} | "
    f"hold={ctx.get('hold_seconds')} | qx={ctx.get('quick_exit_applied')} | "
    f"bot_ct={bot_ct} | kalshi_ct={kalshi_note or '?'} | "
    f"pnl={_profit_usd(t)} | cm={ctx.get('contract_mismatch')} | "
    f"{detail[:70]}"
  )


def main() -> int:
  session = requests.Session()
  has_auth = _login(session)
  if not has_auth:
    print("WARN: no APP_PASSWORD — health-only mode", flush=True)

  state = MonitorState()
  seen_ids: set[str] = set()
  if has_auth:
    for asset in ("btc", "eth"):
      for t in _asset_bundle(session, asset)["trades"]:
        if t.get("id"):
          seen_ids.add(t["id"])

  started = datetime.now(timezone.utc)
  deadline = time.time() + MAX_MINUTES * 60
  poll = 0

  while time.time() <= deadline:
    poll += 1
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    try:
      health = _health(session)
      state.last_version = str(health.get("version") or "?")
      lh = ((health.get("bot_risk") or {}).get("live_hourly") or {})
      mom_bits = []
      for a in ("btc", "eth"):
        mom = (lh.get(a) or {}).get("hour_momentum") or {}
        mom_bits.append(f"{a}_mom={mom.get('state', '?')}")
        cm = (lh.get(a) or {}).get("contract_mismatch")
        if cm:
          mom_bits.append(f"{a}_cm={cm}")

      open_counts: list[str] = []
      if has_auth:
        for asset in ("btc", "eth"):
          bundle = _asset_bundle(session, asset)
          bot = bundle["bot"]
          recon = bundle["reconcile"]
          trades = bundle["trades"]
          open_n = len(bot.get("open_positions") or [])
          recon_ok = recon.get("ok") if recon else None
          recon_label = "OK" if recon_ok is True else ("MISMATCH" if recon_ok is False else "n/a")
          open_counts.append(f"{asset}={open_n}(recon={recon_label})")

          for inc in _check_mismatches(ts, asset, recon):
            state.mismatches.append(inc)
            print(
              f"  MISMATCH {asset} {inc.kind} {inc.ticker} {inc.side} "
              f"bot={inc.bot_contracts} kalshi={inc.kalshi_contracts} delta=+{inc.delta}",
              flush=True,
            )

          enters = _enter_by_position(trades)
          for t in trades:
            tid = t.get("id")
            if not tid or tid in seen_ids:
              continue
            seen_ids.add(tid)
            if t.get("action") != "exit":
              continue
            enter = enters.get(t.get("position_id") or "")
            line = _log_exit(f"{asset}_hourly", t, enter)
            state.exit_log.append(line)
            print(f"  NEW EXIT {line}", flush=True)
            ok, reason = _matches_scalp(t, enter)
            if ok:
              state.scalp_success.append(f"{ts} | {asset} | {reason} | {t.get('label')}")

      summary = (
        f"poll {poll} {ts} | version={state.last_version} | "
        f"open {' '.join(open_counts) or 'n/a'} | {' '.join(mom_bits)}"
      )
      state.poll_summaries.append(summary)
      print(summary, flush=True)
      if state.scalp_success:
        print("SCALP SUCCESS:", state.scalp_success[-1], flush=True)
        break
    except Exception as e:
      err = f"poll {poll} error: {e}"
      state.poll_summaries.append(err)
      print(err, flush=True)

    if state.scalp_success:
      break
    if poll >= 1 and time.time() + POLL_SEC > deadline:
      break
    time.sleep(POLL_SEC)

  print("\n" + "=" * 72, flush=True)
  print("EXTENDED LIVE MONITOR REPORT", flush=True)
  print(f"Window: {started.strftime('%H:%M:%S')} – {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC", flush=True)
  print(f"Target: Beta {TARGET_VERSION} | Observed: {state.last_version}", flush=True)
  print(f"Polls: {poll} @ {POLL_SEC/60:.1f}min | Auth: {has_auth}", flush=True)
  print(f"Scalp criteria: defense, {MIN_HOLD:.0f}-{MAX_HOLD:.0f}s, +${MIN_PROFIT_USD:.2f}+, filled, quick_exit", flush=True)
  print(f"Mismatch incidents: {len(state.mismatches)}", flush=True)
  print(f"New exits logged: {len(state.exit_log)}", flush=True)
  if state.scalp_success:
    print("SCALP RESULT: SUCCESS", flush=True)
    for line in state.scalp_success:
      print(f"  {line}", flush=True)
  else:
    print("SCALP RESULT: criteria not met in window", flush=True)
  if state.mismatches:
    print("\nMismatch log:", flush=True)
    for m in state.mismatches:
      print(
        f"  {m.ts} {m.asset} {m.kind} {m.ticker} {m.side} "
        f"bot={m.bot_contracts} kalshi={m.kalshi_contracts}",
        flush=True,
      )
  if state.exit_log:
    print("\nExit log:", flush=True)
    for line in state.exit_log:
      print(f"  {line}", flush=True)
  print("\nPoll log:", flush=True)
  for s in state.poll_summaries:
    print(f"  {s}", flush=True)
  return 0 if state.scalp_success and not state.mismatches else 1


if __name__ == "__main__":
  raise SystemExit(main())
