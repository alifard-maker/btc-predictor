#!/usr/bin/env python3
"""Compare production BTC hourly bot exits vs Kalshi market settlement (public API)."""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from src.data.kalshi_hourly import fetch_market_row, market_settled
from src.trading.hourly_settlement import (
  contract_spec_from_label,
  contract_spec_from_position,
  settlement_exit_cents,
)
from src.trading.paper_execution import leg_pnl_usd

BASE = os.environ.get(
  "BTC_PREDICTOR_URL",
  "https://btc-predictor-production-f460.up.railway.app",
)
KALSHI_PUBLIC = "https://api.elections.kalshi.com/trade-api/v2"


def _password() -> str:
  env_path = Path(__file__).resolve().parents[1] / ".env"
  if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
      if line.startswith("APP_PASSWORD="):
        return line.split("=", 1)[1].strip()
  return os.environ.get("APP_PASSWORD", "")


def _login(session: requests.Session) -> None:
  r = session.post(f"{BASE}/api/auth/login", data={"password": _password()}, timeout=30)
  if r.status_code not in (200, 302, 303):
    raise RuntimeError(f"login failed: {r.status_code}")


def _fetch_trades(session: requests.Session) -> list[dict[str, Any]]:
  """Pull recent trades plus per-event slices to cover more exits."""
  seen: set[str] = set()
  out: list[dict[str, Any]] = []
  batch = session.get(f"{BASE}/api/hourly/bot/trades?limit=200", timeout=60).json()
  for t in batch.get("trades") or []:
    tid = str(t.get("id") or "")
    if tid and tid not in seen:
      seen.add(tid)
      out.append(t)
  events = sorted({str(t.get("event_ticker") or "") for t in out if t.get("event_ticker")})
  for ev in events[-12:]:
    chunk = session.get(
      f"{BASE}/api/hourly/bot/trades?limit=200&event_ticker={ev}",
      timeout=60,
    ).json()
    for t in chunk.get("trades") or []:
      tid = str(t.get("id") or "")
      if tid and tid not in seen:
        seen.add(tid)
        out.append(t)
  out.sort(key=lambda r: str(r.get("created_at") or ""))
  return out


def _public_market(ticker: str) -> dict[str, Any] | None:
  try:
    r = requests.get(f"{KALSHI_PUBLIC}/markets/{ticker}", timeout=20)
    if r.status_code != 200:
      return None
    data = r.json()
    return data.get("market") or data
  except Exception:
    return None


def _kalshi_settle_cents(
  ticker: str,
  side: str,
  pos_meta: dict[str, Any],
  cache: dict[str, dict | None],
) -> tuple[int | None, str]:
  if ticker not in cache:
    cache[ticker] = _public_market(ticker)
  row = cache[ticker]
  if not row or not market_settled(row):
    return None, "market_open"
  exp = row.get("expiration_value")
  if exp in (None, ""):
    return None, "no_expiration_value"
  try:
    settle_price = float(exp)
  except (TypeError, ValueError):
    return None, "bad_expiration_value"
  spec = contract_spec_from_position(pos_meta) or contract_spec_from_label(pos_meta.get("label"))
  if not spec and row:
    spec = {
      "strike_type": row.get("strike_type"),
      "floor_strike": row.get("floor_strike"),
      "cap_strike": row.get("cap_strike"),
      "contract_type": row.get("contract_type", "threshold"),
    }
  cents = settlement_exit_cents(side=side, settle_price=settle_price, spec=spec)
  if cents is None:
    return None, f"unparsed_strike@{settle_price:.2f}"
  return int(cents), f"settled@{settle_price:,.2f}→{cents}¢"


def _round_trips(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
  """Match enter fills to exits by position_id."""
  enters: dict[str, list[dict]] = defaultdict(list)
  legs: list[dict[str, Any]] = []
  for t in trades:
    if str(t.get("mode") or "") != "live":
      continue
    action = str(t.get("action") or "")
    if action == "enter" and str(t.get("status") or "") == "filled":
      pid = str(t.get("position_id") or "")
      if pid:
        enters[pid].append(t)
    elif action == "exit" and str(t.get("status") or "") in ("filled", "reconciled", "voided"):
      pid = str(t.get("position_id") or "")
      ent = enters[pid][-1] if pid and enters[pid] else None
      legs.append({
        "exit": t,
        "enter": ent,
        "position_id": pid,
      })
  return legs


def build_report(session: requests.Session) -> dict[str, Any]:
  trades = _fetch_trades(session)
  bot = session.get(f"{BASE}/api/hourly/bot", timeout=60).json()
  recon = session.get(f"{BASE}/api/hourly/bot/live-reconcile", timeout=60).json()
  perf = session.get(f"{BASE}/api/bots/performance-report", timeout=60).json()
  kalshi = session.get(f"{BASE}/api/kalshi/status", timeout=30).json()
  health = session.get(f"{BASE}/health", timeout=30).json()
  daily_loss = ((health.get("bot_risk") or {}).get("daily_loss") or {})
  bot_risk_bots = daily_loss.get("bots") or {}

  market_cache: dict[str, dict | None] = {}
  legs_out: list[dict[str, Any]] = []
  bot_closed = 0.0
  kalshi_closed = 0.0
  comparable = 0
  mismatches: list[dict[str, Any]] = []
  voided_pnl = 0.0

  for item in _round_trips(trades):
    ex = item["exit"]
    en = item["enter"]
    status = str(ex.get("status") or "")
    ticker = str(ex.get("market_ticker") or "")
    side = str(ex.get("side") or "")
    contracts = int(ex.get("contracts") or 0)
    entry_c = int((en or ex).get("entry_price_cents") or 0)
    bot_exit_c = ex.get("exit_price_cents")
    bot_pnl = float(ex.get("pnl_usd") or 0) if status != "voided" else 0.0
    if status == "voided":
      voided_pnl += float(ex.get("pnl_usd") or 0)  # should be 0 after cleanup

    pos_meta = {
      "label": (en or ex).get("label"),
      "side": side,
      "floor_strike": (en or ex).get("floor_strike"),
      "cap_strike": (en or ex).get("cap_strike"),
    }
    kalshi_c, kalshi_note = _kalshi_settle_cents(ticker, side, pos_meta, market_cache)
    kalshi_pnl = None
    if kalshi_c is not None and entry_c and contracts and status != "voided":
      kalshi_pnl = float(
        leg_pnl_usd(
          entry_price_cents=entry_c,
          mark_or_exit_cents=kalshi_c,
          contracts=contracts,
        )
        or 0.0,
      )
      kalshi_closed = round(kalshi_closed + kalshi_pnl, 2)
      comparable += 1

    if status in ("filled", "reconciled"):
      bot_closed = round(bot_closed + bot_pnl, 2)

    row = {
      "time": str(ex.get("created_at") or "")[:19],
      "event": ex.get("event_ticker"),
      "ticker": ticker,
      "side": side,
      "contracts": contracts,
      "label": (en or ex).get("label"),
      "entry_c": entry_c,
      "bot_exit_c": bot_exit_c,
      "bot_pnl": round(bot_pnl, 2) if status != "voided" else 0.0,
      "status": status,
      "exit_detail": str(ex.get("detail") or "")[:100],
      "kalshi_exit_c": kalshi_c,
      "kalshi_pnl": round(kalshi_pnl, 2) if kalshi_pnl is not None else None,
      "kalshi_note": kalshi_note,
    }
    legs_out.append(row)
    if (
      kalshi_pnl is not None
      and status in ("filled", "reconciled")
      and abs(bot_pnl - kalshi_pnl) > 0.02
    ):
      mismatches.append({**row, "delta": round(bot_pnl - kalshi_pnl, 2)})

  open_pos = bot.get("open_positions") or []
  open_unreal = round(sum(float(p.get("unrealized_pnl_usd") or 0) for p in open_pos), 2)
  open_cost = round(sum(float(p.get("cost_usd") or 0) for p in open_pos), 2)

  perf_btc = next(
    (b for b in (perf.get("bots") or []) if b.get("kind") == "hourly" and b.get("asset") == "btc"),
    {},
  )

  from datetime import timedelta
  from zoneinfo import ZoneInfo

  ny = ZoneInfo("America/New_York")
  now_utc = datetime.now(timezone.utc)
  cutoff_24h = now_utc - timedelta(hours=24)
  today_ny = now_utc.astimezone(ny).date().isoformat()

  def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
      return None
    try:
      ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
      return None
    if ts.tzinfo is None:
      ts = ts.replace(tzinfo=timezone.utc)
    return ts

  pnl_24h = 0.0
  pnl_today_et = 0.0
  exits_24h: list[dict[str, Any]] = []
  for item in _round_trips(trades):
    ex = item["exit"]
    status = str(ex.get("status") or "")
    if status not in ("filled", "reconciled"):
      continue
    ts = _parse_ts(str(ex.get("created_at") or ""))
    pnl = float(ex.get("pnl_usd") or 0)
    if ts and ts >= cutoff_24h:
      pnl_24h = round(pnl_24h + pnl, 2)
      exits_24h.append({
        "time": str(ex.get("created_at") or "")[:19],
        "asset": "btc",
        "pnl_usd": round(pnl, 2),
        "event": ex.get("event_ticker"),
        "detail": str(ex.get("detail") or "")[:80],
      })
    if ts and ts.astimezone(ny).date().isoformat() == today_ny:
      pnl_today_et = round(pnl_today_et + pnl, 2)

  risk_btc = bot_risk_bots.get("hourly:btc") or {}
  risk_eth = bot_risk_bots.get("hourly:eth") or {}

  return {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "event_ticker": bot.get("event_ticker"),
    "kalshi_cash_usd": kalshi.get("balance_usd"),
    "brti_live": kalshi.get("brti_live"),
    "reconcile_ok": recon.get("ok"),
    "reconcile_legs": recon.get("bot_live_legs"),
    "performance_report": perf_btc.get("summary"),
    "daily_risk_gate": {
      "date_key": daily_loss.get("date_key"),
      "timezone": daily_loss.get("timezone"),
      "hourly_btc": risk_btc,
      "hourly_eth": risk_eth,
    },
    "trade_log_pnl_today_et": {
      "btc_hourly": pnl_today_et,
      "exit_count": len([e for e in exits_24h if e.get("time", "")[:10] == today_ny[:10]]),
    },
    "trade_log_pnl_24h": {
      "btc_hourly": pnl_24h,
      "exit_count": len(exits_24h),
      "exits": exits_24h[-20:],
    },
    "bot_closed_pnl_sampled": bot_closed,
    "kalshi_settle_pnl_comparable": kalshi_closed,
    "comparable_settled_legs": comparable,
    "mismatch_count": len(mismatches),
    "voided_exits": sum(1 for x in legs_out if x["status"] == "voided"),
    "open_positions": len(open_pos),
    "open_unrealized_usd": open_unreal,
    "open_cost_usd": open_cost,
    "net_bot_mark_to_market": round(bot_closed + open_unreal, 2),
    "legs": legs_out,
    "mismatches": mismatches,
  }


def main() -> int:
  if not _password():
    print("Set APP_PASSWORD", file=sys.stderr)
    return 1
  session = requests.Session()
  _login(session)
  report = build_report(session)
  print(json.dumps(report, indent=2))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
