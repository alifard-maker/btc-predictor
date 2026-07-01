#!/usr/bin/env python3
"""Overlay production live trades on replay deploy comparison."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.replay_hourly_event import compute_trade_stats

BASE = "https://btc-predictor-production-f460.up.railway.app"


def _password() -> str:
  env_path = Path(__file__).resolve().parents[1] / ".env"
  if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
      if line.startswith("APP_PASSWORD="):
        return line.split("=", 1)[1].strip()
  return ""


def _login(session: requests.Session) -> None:
  r = session.post(f"{BASE}/api/auth/login", data={"password": _password()}, timeout=30)
  if r.status_code not in (200, 302, 303):
    raise RuntimeError(f"login failed: {r.status_code}")


def _fetch_event_trades(session: requests.Session, event_ticker: str) -> list[dict[str, Any]]:
  chunk = session.get(
    f"{BASE}/api/hourly/bot/trades",
    params={"limit": 200, "event_ticker": event_ticker},
    timeout=60,
  ).json()
  return chunk.get("trades") or []


def _fetch_all_live_trades(session: requests.Session) -> list[dict[str, Any]]:
  """Best-effort pull of production hourly live trades."""
  seen: set[str] = set()
  out: list[dict[str, Any]] = []
  batch = session.get(f"{BASE}/api/hourly/bot/trades", params={"limit": 200}, timeout=60).json()
  for t in batch.get("trades") or []:
    tid = str(t.get("id") or "")
    if tid and tid not in seen:
      seen.add(tid)
      out.append(t)
  for ev in sorted({str(t.get("event_ticker") or "") for t in out if t.get("event_ticker")}):
    for t in _fetch_event_trades(session, ev):
      tid = str(t.get("id") or "")
      if tid and tid not in seen:
        seen.add(tid)
        out.append(t)
  out.sort(key=lambda r: str(r.get("created_at") or ""))
  return out


def _exit_reason(detail: str | None) -> str:
  text = (detail or "").upper()
  if "TAKE PROFIT" in text or "PROFIT TRAIL" in text:
    return "TAKE PROFIT"
  if "CUT LOSS" in text or "CUT LOSSES" in text:
    return "CUT LOSSES"
  if "CHEAP LEG" in text:
    return "CHEAP LEG CUT LOSS"
  if "RECONCIL" in text:
    return "RECONCILED"
  if "SETTLEMENT" in text or "SETTLE" in text:
    return "SETTLEMENT"
  return "OTHER"


def _live_hour_stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
  log: list[dict[str, Any]] = []
  for t in sorted(trades, key=lambda x: str(x.get("created_at") or "")):
    act = t.get("action")
    if act == "enter":
      log.append({
        "action": "enter",
        "status": t.get("status") or "filled",
        "side": t.get("side"),
        "label": t.get("label"),
        "contracts": t.get("contracts"),
        "price_cents": t.get("price_cents"),
      })
    elif act == "exit" and t.get("pnl_usd") is not None:
      log.append({
        "action": "exit",
        "reason": _exit_reason(t.get("detail")),
        "pnl_usd": float(t["pnl_usd"]),
        "side": t.get("side"),
        "label": t.get("label"),
      })

  stats = compute_trade_stats(log)
  realized = round(sum(float(t.get("pnl_usd") or 0) for t in trades if t.get("action") == "exit"), 2)
  by_reason: dict[str, dict[str, float | int]] = defaultdict(lambda: {"n": 0, "pnl": 0.0})
  for row in log:
    if row.get("action") != "exit":
      continue
    k = str(row.get("reason") or "OTHER")
    by_reason[k]["n"] += 1
    by_reason[k]["pnl"] += float(row.get("pnl_usd") or 0)
  for k in by_reason:
    by_reason[k]["pnl"] = round(by_reason[k]["pnl"], 2)

  filled_live = sum(
    1 for t in trades
    if t.get("action") == "enter" and t.get("status") == "filled" and (t.get("mode") or "") == "live"
  )
  resting_live = sum(
    1 for t in trades
    if t.get("action") == "enter" and t.get("status") == "resting" and (t.get("mode") or "") == "live"
  )

  return {
    "realized_pnl_usd": realized,
    "trade_stats": {
      **stats,
      "filled_enters": filled_live,
      "resting_enters": resting_live,
      "total_enters_attempted": filled_live + resting_live,
    },
    "summary_by_reason": dict(by_reason),
    "n_trade_rows": len(trades),
  }


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
  if not rows:
    return {"hours": 0, "total_pnl_usd": 0.0, "filled_enters": 0, "wins": 0, "losses": 0}
  filled = sum(r["trade_stats"]["filled_enters"] for r in rows)
  resting = sum(r["trade_stats"]["resting_enters"] for r in rows)
  wins = sum(r["trade_stats"]["wins"] for r in rows)
  losses = sum(r["trade_stats"]["losses"] for r in rows)
  flats = sum(r["trade_stats"]["flats"] for r in rows)
  closed = wins + losses + flats
  total_pnl = round(sum(r["realized_pnl_usd"] for r in rows), 2)
  by_reason: dict[str, dict[str, float | int]] = defaultdict(lambda: {"n": 0, "pnl": 0.0})
  for row in rows:
    for k, v in (row.get("summary_by_reason") or {}).items():
      by_reason[k]["n"] += int(v["n"])
      by_reason[k]["pnl"] += float(v["pnl"])
  for k in by_reason:
    by_reason[k]["pnl"] = round(by_reason[k]["pnl"], 2)
  return {
    "hours": len(rows),
    "filled_enters": filled,
    "resting_enters": resting,
    "exits": sum(r["trade_stats"]["exits"] for r in rows),
    "wins": wins,
    "losses": losses,
    "flats": flats,
    "win_rate": round(wins / closed, 4) if closed else 0.0,
    "total_pnl_usd": total_pnl,
    "avg_pnl_per_hour_usd": round(total_pnl / len(rows), 2),
    "winning_hours": sum(1 for r in rows if r["realized_pnl_usd"] > 0),
    "losing_hours": sum(1 for r in rows if r["realized_pnl_usd"] < 0),
    "flat_hours": sum(1 for r in rows if r["realized_pnl_usd"] == 0),
    "by_exit_type": dict(by_reason),
  }


def overlay(
  replay_path: Path,
  output: Path | None = None,
) -> dict[str, Any]:
  replay = json.loads(replay_path.read_text())
  events: list[str] = []
  for profile in replay.get("profiles", {}).values():
    for h in profile.get("hours") or []:
      ev = h.get("event_ticker")
      if ev and ev not in events:
        events.append(ev)

  session = requests.Session()
  _login(session)

  all_trades = _fetch_all_live_trades(session)
  by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
  for t in all_trades:
    ev = str(t.get("event_ticker") or "")
    if ev:
      by_event[ev].append(t)

  live_hours: list[dict[str, Any]] = []
  for ev in events:
    trades = by_event.get(ev) or _fetch_event_trades(session, ev)
    if not trades:
      continue
    hour = _live_hour_stats(trades)
    hour["event_ticker"] = ev
    live_hours.append(hour)

  live_agg = _aggregate(live_hours)
  comparison: dict[str, Any] = {}
  for name, block in replay.get("profiles", {}).items():
    rep_agg = block.get("aggregate") or {}
    comparison[name] = {
      "pnl_delta_replay_minus_live": round(
        float(rep_agg.get("total_pnl_usd") or 0) - live_agg["total_pnl_usd"], 2,
      ),
      "fills_delta_replay_minus_live": int(rep_agg.get("filled_enters") or 0) - live_agg["filled_enters"],
    }

  out = {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "source_replay": str(replay_path),
    "replay_window": replay.get("replay_window_utc"),
    "events_in_replay": len(events),
    "events_with_live_trades": len(live_hours),
    "production_trade_rows_fetched": len(all_trades),
    "production_live": {
      "label": "Actual Kalshi live trades (production API)",
      "aggregate": live_agg,
      "hours": live_hours,
    },
    "replay_vs_live": comparison,
    "side_by_side": {
      "live": live_agg,
      **{name: block.get("aggregate") for name, block in replay.get("profiles", {}).items()},
    },
  }

  out_path = output or replay_path.with_name("replay_deploy_compare_10d_with_live.json")
  out_path.write_text(json.dumps(out, indent=2))
  print(f"Wrote {out_path}")
  print(
    f"Live: ${live_agg['total_pnl_usd']:+.2f} over {live_agg['hours']}h | "
    f"fills={live_agg['filled_enters']} W/L={live_agg['wins']}/{live_agg['losses']}"
  )
  for name, delta in comparison.items():
    print(f"  {name} vs live: pnl Δ ${delta['pnl_delta_replay_minus_live']:+.2f}, fills Δ {delta['fills_delta_replay_minus_live']:+d}")
  return out


def main() -> None:
  p = argparse.ArgumentParser()
  p.add_argument(
    "--replay",
    default="data/logs/replay_deploy_compare_10d.json",
    help="Replay comparison JSON",
  )
  p.add_argument("--output", default=None)
  args = p.parse_args()
  overlay(Path(args.replay), Path(args.output) if args.output else None)


if __name__ == "__main__":
  main()
