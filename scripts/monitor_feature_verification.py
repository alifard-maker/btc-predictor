#!/usr/bin/env python3
"""Poll production for quick_exit, exit verification, inventory sync, and rule health.

Cron example (30 min production verification after deploy):
  MONITOR_MAX_MIN=30 MONITOR_TARGET_VERSION=4.0.40 python scripts/monitor_feature_verification.py
"""

from __future__ import annotations

import json
import os
import re
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
POLL_SEC = float(os.environ.get("MONITOR_POLL_SEC", "150"))  # 2.5 min
DEFAULT_MAX_MINUTES = float(os.environ.get("MONITOR_MAX_MIN", "30"))
MAX_MINUTES = DEFAULT_MAX_MINUTES
TARGET_VERSION = os.environ.get("MONITOR_TARGET_VERSION", "4.0.40")
LOG_PATH = Path(os.environ.get("MONITOR_LOG", "/tmp/btc_monitor_verification.jsonl"))
ASSETS = tuple(
  a.strip()
  for a in os.environ.get("MONITOR_ASSETS", "btc,eth,spx,ndx").split(",")
  if a.strip()
)


def _password() -> str:
  env_path = Path(__file__).resolve().parents[1] / ".env"
  if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
      if line.startswith("APP_PASSWORD="):
        return line.split("=", 1)[1].strip()
  return os.environ.get("APP_PASSWORD", "")


def _login(session: requests.Session) -> None:
  pw = _password()
  if not pw:
    raise RuntimeError("APP_PASSWORD missing")
  r = session.post(f"{BASE}/api/auth/login", data={"password": pw}, allow_redirects=False, timeout=30)
  if r.status_code not in (200, 302, 303):
    raise RuntimeError(f"login failed: {r.status_code}")


def _parse_ts(s: str | None) -> datetime | None:
  if not s:
    return None
  try:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))
  except ValueError:
    return None


def _pnl_from_detail(detail: str) -> tuple[float | None, float | None]:
  pct_m = re.search(r"\+(\d+\.?\d*)%", detail)
  usd_m = re.search(r"\+?\$([0-9]+\.[0-9]+)", detail)
  pct = float(pct_m.group(1)) / 100 if pct_m else None
  usd = float(usd_m.group(1)) if usd_m else None
  if "-$" in detail or "CUT" in detail.upper():
    loss_m = re.search(r"-?\$([0-9]+\.[0-9]+)", detail)
    if loss_m:
      usd = -float(loss_m.group(1))
  return pct, usd


@dataclass
class FeatureStatus:
  status: str = "not yet"
  evidence: list[str] = field(default_factory=list)


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
  entry_source: str | None = None


@dataclass
class MonitorState:
  started_at: datetime
  seen_trade_ids: set[str] = field(default_factory=set)
  quick_scalp: FeatureStatus = field(default_factory=FeatureStatus)
  exit_verification: FeatureStatus = field(default_factory=FeatureStatus)
  soft_rally: FeatureStatus = field(default_factory=FeatureStatus)
  inventory_sync: FeatureStatus = field(default_factory=FeatureStatus)
  resting_adopt_sync: FeatureStatus = field(default_factory=FeatureStatus)
  rule_conflicts: FeatureStatus = field(default_factory=FeatureStatus)
  mismatches: list[MismatchIncident] = field(default_factory=list)
  quick_exit_exits: list[str] = field(default_factory=list)
  poll_count: int = 0
  last_health: dict[str, Any] = field(default_factory=dict)


def _asset_prefix(asset: str) -> str:
  return "/api/hourly" if asset == "btc" else f"/api/{asset}/hourly"


def _parse_trades_payload(data: Any) -> list[dict]:
  if isinstance(data, list):
    return [t for t in data if isinstance(t, dict)]
  if isinstance(data, dict):
    return [t for t in (data.get("trades") or []) if isinstance(t, dict)]
  return []


def _asset_bundle(session: requests.Session, asset: str) -> dict[str, Any]:
  prefix = _asset_prefix(asset)
  bot = session.get(f"{BASE}{prefix}/bot", timeout=30).json()
  recon: dict[str, Any] = {}
  rr = session.get(f"{BASE}{prefix}/bot/live-reconcile", timeout=30)
  if rr.ok:
    recon = rr.json()
  if not recon:
    recon = bot.get("live_reconcile") or {}
  trades_raw = session.get(f"{BASE}{prefix}/bot/trades?limit=200", timeout=60).json()
  trades = _parse_trades_payload(trades_raw)
  return {
    "bot": bot,
    "reconcile": recon,
    "trades": [t for t in trades if t.get("mode") == "live"],
  }


def _trade_sources(session: requests.Session) -> list[tuple[str, list[dict]]]:
  out: list[tuple[str, list[dict]]] = []
  for asset in ASSETS:
    try:
      bundle = _asset_bundle(session, asset)
      out.append((f"{asset}_hourly", bundle["trades"]))
    except Exception as e:
      out.append((f"{asset}_hourly", []))
      print(f"WARN: {asset} trades unavailable: {e}", flush=True)
  return out


def _bot_status(session: requests.Session) -> dict[str, Any]:
  out: dict[str, Any] = {}
  for asset in ASSETS:
    prefix = _asset_prefix(asset)
    resp = session.get(f"{BASE}{prefix}/bot", timeout=30)
    if resp.ok:
      out[asset] = resp.json()
  return out


def _health(session: requests.Session) -> dict[str, Any]:
  return session.get(f"{BASE}/health", timeout=30).json()


def _enter_by_position(trades: list[dict]) -> dict[str, dict]:
  m: dict[str, dict] = {}
  for t in trades:
    if t.get("action") != "enter":
      continue
    pid = t.get("position_id")
    if pid:
      m[pid] = t
  return m


def _hold_seconds(enter: dict | None, exit_trade: dict) -> float | None:
  ctx = exit_trade.get("exit_context") or {}
  if ctx.get("hold_seconds") is not None:
    return float(ctx["hold_seconds"])
  if not enter:
    return None
  t0 = _parse_ts(enter.get("created_at"))
  t1 = _parse_ts(exit_trade.get("created_at"))
  if t0 and t1:
    return (t1 - t0).total_seconds()
  return None


def _is_defense_or_conservative(trade: dict) -> bool:
  es = trade.get("entry_settings") or {}
  adaptive = es.get("adaptive") or {}
  mode = str(adaptive.get("entry_mode") or "").lower()
  if mode == "defense":
    return True
  hm = es.get("hour_momentum") or {}
  return str(hm.get("state") or "").lower() == "conservative"


def _soft_rally_entry_ok(trade: dict) -> bool:
  if trade.get("action") != "enter":
    return False
  es = trade.get("entry_settings") or {}
  adaptive = es.get("adaptive") or {}
  if str(adaptive.get("entry_mode") or "").lower() != "defense":
    return False
  side = str(trade.get("side") or "").lower()
  if side != "yes":
    return False
  price = trade.get("entry_price_cents") or trade.get("price_cents")
  if price is None:
    return False
  if not (40 <= int(price) <= 80):
    return False
  detail = (trade.get("detail") or "").lower()
  if "defense_skip_all" in detail:
    return False
  return True


def _check_resting_adopt_mismatches(
  ts: str,
  asset: str,
  bot: dict[str, Any],
  recon: dict[str, Any],
) -> list[MismatchIncident]:
  """Alert when resting-adopted legs track fewer contracts than Kalshi inventory."""
  out: list[MismatchIncident] = []
  kalshi_by_key: dict[tuple[str, str], float] = {}
  for row in (recon.get("matched") or []) + (recon.get("mismatches") or []):
    ticker = str(row.get("ticker") or "")
    side = str(row.get("side") or "").lower()
    kalshi_ct = row.get("kalshi_contracts")
    if ticker and side and kalshi_ct is not None:
      kalshi_by_key[(ticker, side)] = float(kalshi_ct)
  for row in recon.get("kalshi_only") or []:
    ticker = str(row.get("ticker") or "")
    side = str(row.get("side") or "").lower()
    if ticker and side:
      kalshi_by_key[(ticker, side)] = float(row.get("contracts") or 0)

  for pos in bot.get("open_positions") or []:
    if pos.get("mode") != "live":
      continue
    src = str(pos.get("entry_source") or "")
    if not src.startswith("adopted_resting"):
      continue
    ticker = str(pos.get("market_ticker") or "")
    side = str(pos.get("side") or "").lower()
    bot_ct = float(pos.get("contracts_fp") or pos.get("contracts") or 0)
    kalshi_ct = kalshi_by_key.get((ticker, side))
    if kalshi_ct is None:
      continue
    if kalshi_ct > bot_ct + 0.24:
      out.append(MismatchIncident(
        ts=ts,
        asset=asset,
        ticker=ticker,
        side=side,
        bot_contracts=bot_ct,
        kalshi_contracts=kalshi_ct,
        delta=round(kalshi_ct - bot_ct, 2),
        kind="resting_adopt_undercount",
        entry_source=src,
      ))
  return out


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


def _detect_rule_conflict(skip: str) -> str | None:
  s = skip.lower()
  if "defense_skip_all" in s and "soft_rally" in s:
    return "soft_rally vs defense_skip_all"
  if "hour_momentum:pressing" in s and "adaptive_defense" in s:
    return "hour_momentum pressing vs adaptive defense"
  if "max_concurrent" in s and "fully_deployed" in s:
    return "max_concurrent vs fully_deployed"
  if "adaptive_defense_skip" in s and "soft_rally" in s:
    return "adaptive defense skip with soft_rally gate"
  return None


def _log_quick_exit_exit(state: MonitorState, source: str, t: dict, enter: dict | None) -> None:
  ctx = t.get("exit_context") or {}
  if not ctx.get("quick_exit_applied"):
    return
  hold = _hold_seconds(enter, t)
  _, pnl = _pnl_from_detail(t.get("detail") or "")
  if t.get("pnl_usd") is not None:
    pnl = float(t["pnl_usd"])
  verified = (
    t.get("status") == "filled"
    and "unverified" not in (t.get("detail") or "").lower()
  )
  hold_s = f"{hold:.0f}s" if hold is not None else "?"
  line = (
    f"{(t.get('created_at') or '')[:19]} | {source} | status={t.get('status')} | "
    f"hold={hold_s} | pnl=${pnl if pnl is not None else '?'} | verified={verified} | "
    f"qx=true | {(t.get('detail') or '')[:80]}"
  )
  state.quick_exit_exits.append(line)


def _check_new_trades(state: MonitorState, source: str, trades: list[dict]) -> None:
  enters = _enter_by_position(trades)
  for t in trades:
    tid = t.get("id")
    if not tid or tid in state.seen_trade_ids:
      continue
    state.seen_trade_ids.add(tid)
    ca = (t.get("created_at") or "")[:19]
    detail = t.get("detail") or ""
    action = t.get("action")
    status = t.get("status")

    if action == "enter" and status == "filled" and _soft_rally_entry_ok(t):
      es = t.get("entry_settings") or {}
      state.soft_rally.status = "verified"
      state.soft_rally.evidence.append(
        f"{ca} UTC | {source} | trade {tid[:8]} | defense YES {t.get('entry_price_cents')}¢ "
        f"{t.get('label','')} | entry_mode={es.get('adaptive',{}).get('entry_mode')}"
      )

    if action != "exit":
      continue

    enter = enters.get(t.get("position_id") or "")
    hold = _hold_seconds(enter, t)
    _, pnl_usd = _pnl_from_detail(detail)
    is_unverified = "unverified" in detail.lower() and "inventory unchanged" in detail.lower()
    is_backfill = "backfilled" in detail.lower()
    is_filled = status == "filled"
    defense = _is_defense_or_conservative(enter) if enter else False

    _log_quick_exit_exit(state, source, t, enter)

    if is_unverified:
      if state.exit_verification.status != "verified":
        state.exit_verification.status = "broken"
      state.exit_verification.evidence.append(
        f"{ca} UTC | {source} | trade {tid[:8]} | UNVERIFIED exit — {detail[:100]}"
      )

    if is_filled and not is_backfill and not is_unverified:
      state.exit_verification.status = "verified"
      state.exit_verification.evidence.append(
        f"{ca} UTC | {source} | trade {tid[:8]} | filled exit (IOC verified) — {detail[:120]}"
      )

    if (
      is_filled
      and not is_backfill
      and not is_unverified
      and defense
      and hold is not None
      and 30 <= hold <= 90
    ):
      profit_ok = pnl_usd is not None and pnl_usd >= 0.06
      ctx = t.get("exit_context") or {}
      if profit_ok and ctx.get("quick_exit_applied"):
        state.quick_scalp.status = "verified"
        state.quick_scalp.evidence.append(
          f"{ca} UTC | {source} | trade {tid[:8]} | hold={hold:.0f}s defense pnl=${pnl_usd:.2f} qx=true — {detail[:120]}"
        )


def _check_health_skips(state: MonitorState, health: dict) -> None:
  lh = (health.get("bot_risk") or {}).get("live_hourly") or {}
  for asset in ASSETS:
    bot = lh.get(asset) or {}
    skip = str(bot.get("last_skip_reason") or "")
    if not skip:
      continue
    conflict = _detect_rule_conflict(skip)
    if conflict:
      state.rule_conflicts.status = "flagged"
      state.rule_conflicts.evidence.append(f"health | {asset} skip={skip} | conflict={conflict}")
    if "soft_rally" in skip and "defense_skip_all" not in skip:
      state.soft_rally.evidence.append(
        f"health | {asset} skip={skip} (gates active, awaiting passing entry)"
      )


def _check_inventory(state: MonitorState, ts: str, session: requests.Session) -> None:
  for asset in ASSETS:
    try:
      bundle = _asset_bundle(session, asset)
    except Exception:
      continue
    recon = bundle["reconcile"]
    bot = bundle["bot"]
    if recon.get("ok") is True:
      state.inventory_sync.status = "verified"
      state.inventory_sync.evidence.append(f"{ts} | {asset} reconcile OK")
    incidents = _check_mismatches(ts, asset, recon)
    resting_incidents = _check_resting_adopt_mismatches(ts, asset, bot, recon)
    if resting_incidents:
      state.resting_adopt_sync.status = "mismatch"
      state.mismatches.extend(resting_incidents)
      for inc in resting_incidents:
        state.resting_adopt_sync.evidence.append(
          f"{ts} | {asset} resting_adopt {inc.ticker} {inc.side} "
          f"bot={inc.bot_contracts} kalshi={inc.kalshi_contracts} delta=+{inc.delta}"
        )
    elif any(
      str(p.get("entry_source") or "").startswith("adopted_resting")
      for p in (bot.get("open_positions") or [])
      if p.get("mode") == "live"
    ):
      state.resting_adopt_sync.status = "verified"
      state.resting_adopt_sync.evidence.append(f"{ts} | {asset} resting-adopt legs aligned")
    if incidents:
      state.inventory_sync.status = "mismatch"
      state.mismatches.extend(incidents)
      for inc in incidents:
        state.inventory_sync.evidence.append(
          f"{ts} | {asset} {inc.kind} {inc.ticker} {inc.side} "
          f"bot={inc.bot_contracts} kalshi={inc.kalshi_contracts} delta=+{inc.delta}"
        )


def _log(state: MonitorState, msg: str, extra: dict | None = None) -> None:
  row = {
    "ts": datetime.now(timezone.utc).isoformat(),
    "poll": state.poll_count,
    "msg": msg,
    **(extra or {}),
  }
  with LOG_PATH.open("a", encoding="utf-8") as f:
    f.write(json.dumps(row) + "\n")
  print(f"[{row['ts'][:19]}] poll={state.poll_count} {msg}", flush=True)


def _all_verified(state: MonitorState) -> bool:
  return all(
    getattr(state, k).status == "verified"
    for k in ("quick_scalp", "exit_verification", "soft_rally", "inventory_sync", "resting_adopt_sync")
  ) and state.rule_conflicts.status != "flagged"


def _print_report(state: MonitorState) -> None:
  print("\n" + "=" * 72)
  print("FINAL VERIFICATION REPORT")
  print(f"Started: {state.started_at.isoformat()[:19]} UTC | Polls: {state.poll_count}")
  print(f"Target: Beta {TARGET_VERSION} | Version: {state.last_health.get('version', '?')}")
  print("=" * 72)
  rows = [
    ("quick scalp", state.quick_scalp),
    ("exit verification", state.exit_verification),
    ("soft_rally", state.soft_rally),
    ("inventory sync", state.inventory_sync),
    ("resting adopt sync", state.resting_adopt_sync),
    ("rule conflicts", state.rule_conflicts),
  ]
  print("| Feature | Status | Evidence |")
  print("|---|---|---|")
  for name, feat in rows:
    ev = "; ".join(feat.evidence[-3:]) if feat.evidence else "(none during window)"
    print(f"| {name} | {feat.status} | {ev} |")
  if state.mismatches:
    print(f"\nMismatch incidents: {len(state.mismatches)}")
    for m in state.mismatches[-5:]:
      print(
        f"  {m.ts} {m.asset} {m.kind} {m.ticker} {m.side} "
        f"bot={m.bot_contracts} kalshi={m.kalshi_contracts}",
      )
  if state.quick_exit_exits:
    print(f"\nQuick-exit exits ({len(state.quick_exit_exits)}):")
    for line in state.quick_exit_exits[-8:]:
      print(f"  {line}")
  print()


def main() -> int:
  session = requests.Session()
  _login(session)
  state = MonitorState(started_at=datetime.now(timezone.utc))
  LOG_PATH.write_text("", encoding="utf-8")

  for _, trades in _trade_sources(session):
    for t in trades:
      if t.get("id"):
        state.seen_trade_ids.add(t["id"])

  health = _health(session)
  state.last_health = health
  _check_health_skips(state, health)
  ts0 = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
  _check_inventory(state, ts0, session)
  _log(
    state,
    f"monitor start | version={health.get('version')} | assets={','.join(ASSETS)} | "
    f"baseline_trades={len(state.seen_trade_ids)}",
  )

  deadline = time.time() + MAX_MINUTES * 60
  while time.time() < deadline:
    state.poll_count += 1
    try:
      health = _health(session)
      state.last_health = health
      _check_health_skips(state, health)
      bots = _bot_status(session)
      open_n = sum(len((bots.get(k) or {}).get("open_positions") or []) for k in bots)
      ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
      _check_inventory(state, ts, session)
      for source, trades in _trade_sources(session):
        _check_new_trades(state, source, trades)
      _log(
        state,
        f"poll | open={open_n} | qs={state.quick_scalp.status} ev={state.exit_verification.status} "
        f"sr={state.soft_rally.status} inv={state.inventory_sync.status} "
        f"radopt={state.resting_adopt_sync.status} "
        f"rules={state.rule_conflicts.status} mismatches={len(state.mismatches)}",
      )
      if _all_verified(state):
        _log(state, "all features verified — stopping early")
        break
    except Exception as e:
      _log(state, f"poll error: {e}")
    if _all_verified(state):
      break
    time.sleep(POLL_SEC)

  _print_report(state)
  return (
    0
    if state.inventory_sync.status != "mismatch"
    and state.resting_adopt_sync.status != "mismatch"
    and state.rule_conflicts.status != "flagged"
    else 1
  )


if __name__ == "__main__":
  raise SystemExit(main())
