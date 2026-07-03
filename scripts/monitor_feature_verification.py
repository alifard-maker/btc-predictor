#!/usr/bin/env python3
"""Poll production for quick_exit, exit verification, and soft_rally evidence."""

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
MAX_MINUTES = float(os.environ.get("MONITOR_MAX_MIN", "40"))
LOG_PATH = Path(os.environ.get("MONITOR_LOG", "/tmp/btc_monitor_verification.jsonl"))


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
class MonitorState:
  started_at: datetime
  seen_trade_ids: set[str] = field(default_factory=set)
  quick_scalp: FeatureStatus = field(default_factory=FeatureStatus)
  exit_verification: FeatureStatus = field(default_factory=FeatureStatus)
  soft_rally: FeatureStatus = field(default_factory=FeatureStatus)
  poll_count: int = 0
  last_health: dict[str, Any] = field(default_factory=dict)


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


def _bot_status(session: requests.Session) -> dict[str, Any]:
  out: dict[str, Any] = {}
  for key, path in (
    ("btc", "/api/hourly/bot"),
    ("eth", "/api/eth/hourly/bot"),
  ):
    resp = session.get(f"{BASE}{path}", timeout=30)
    if resp.ok:
      out[key] = resp.json()
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
    return None
  if not (40 <= int(price) <= 80):
    return False
  detail = (trade.get("detail") or "").lower()
  if "defense_skip_all" in detail:
    return False
  return True


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

    # soft_rally: successful defense YES mid-band entry
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
    is_reconcile_only = status == "reconciled"
    is_filled = status == "filled"
    defense = _is_defense_or_conservative(enter) if enter else False

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

    # quick scalp: defense + hold 30-119s + profit ~$0.06+ or cut ~$0.12 + filled non-backfill
    if (
      is_filled
      and not is_backfill
      and not is_unverified
      and defense
      and hold is not None
      and 30 <= hold < 120
    ):
      profit_ok = pnl_usd is not None and pnl_usd >= 0.06
      cut_ok = pnl_usd is not None and abs(pnl_usd) >= 0.10 and "CUT" in detail.upper()
      if profit_ok or cut_ok:
        state.quick_scalp.status = "verified"
        state.quick_scalp.evidence.append(
          f"{ca} UTC | {source} | trade {tid[:8]} | hold={hold:.0f}s defense pnl=${pnl_usd:.2f} — {detail[:120]}"
        )


def _check_health_skips(state: MonitorState, health: dict) -> None:
  lh = (health.get("bot_risk") or {}).get("live_hourly") or {}
  for asset in ("btc", "eth"):
    bot = lh.get(asset) or {}
    skip = str(bot.get("last_skip_reason") or "")
    if "soft_rally" in skip and "defense_skip_all" not in skip:
      # gates active; note but don't verify until entry passes
      state.soft_rally.evidence.append(
        f"health | {asset} skip={skip} (gates active, awaiting passing entry)"
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
    for k in ("quick_scalp", "exit_verification", "soft_rally")
  )


def _print_report(state: MonitorState) -> None:
  print("\n" + "=" * 72)
  print("FINAL VERIFICATION REPORT")
  print(f"Started: {state.started_at.isoformat()[:19]} UTC | Polls: {state.poll_count}")
  print(f"Version: {state.last_health.get('version', '?')}")
  print("=" * 72)
  rows = [
    ("quick scalp", state.quick_scalp),
    ("exit verification", state.exit_verification),
    ("soft_rally", state.soft_rally),
  ]
  print("| Feature | Status | Evidence |")
  print("|---|---|---|")
  for name, feat in rows:
    ev = "; ".join(feat.evidence[-3:]) if feat.evidence else "(none during window)"
    print(f"| {name} | {feat.status} | {ev} |")
  print()


def main() -> int:
  session = requests.Session()
  _login(session)
  state = MonitorState(started_at=datetime.now(timezone.utc))
  LOG_PATH.write_text("", encoding="utf-8")

  # Baseline existing trade IDs so we only watch NEW events
  for _, trades in _trade_sources(session):
    for t in trades:
      if t.get("id"):
        state.seen_trade_ids.add(t["id"])

  health = _health(session)
  state.last_health = health
  _check_health_skips(state, health)
  _log(
    state,
    f"monitor start | version={health.get('version')} | baseline_trades={len(state.seen_trade_ids)}",
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
      for source, trades in _trade_sources(session):
        _check_new_trades(state, source, trades)
      _log(
        state,
        f"poll | open={open_n} | qs={state.quick_scalp.status} ev={state.exit_verification.status} sr={state.soft_rally.status}",
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
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
