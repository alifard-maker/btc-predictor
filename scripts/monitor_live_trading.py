#!/usr/bin/env python3
"""Watch live bot vs Kalshi alignment on production (or local)."""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

DEFAULT_BASE = os.environ.get(
  "BTC_PREDICTOR_URL",
  "https://btc-predictor-production-f460.up.railway.app",
)


def _password() -> str:
  env_path = Path(__file__).resolve().parents[1] / ".env"
  if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
      if line.startswith("APP_PASSWORD="):
        return line.split("=", 1)[1].strip()
  return os.environ.get("APP_PASSWORD", "")


def _login(session: requests.Session, base: str, password: str) -> None:
  r = session.post(f"{base}/api/auth/login", data={"password": password}, allow_redirects=False, timeout=30)
  if r.status_code not in (200, 302, 303):
    raise RuntimeError(f"login failed: {r.status_code} {r.text[:200]}")


def _fetch(session: requests.Session, base: str) -> tuple[dict, dict, dict]:
  bot = session.get(f"{base}/api/hourly/bot", timeout=30).json()
  recon = session.get(f"{base}/api/hourly/bot/live-reconcile", timeout=30).json()
  kalshi = session.get(f"{base}/api/kalshi/status", timeout=30).json()
  return bot, recon, kalshi


def _fmt_leg(row: dict) -> str:
  labels = row.get("labels") or []
  label = labels[0] if labels else row.get("ticker", "?")
  return f"{row.get('side', '?').upper()} {label} x{row.get('contracts', '?')}"


def _print_snapshot(bot: dict, recon: dict, kalshi: dict) -> bool:
  ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
  settings = bot.get("settings") or {}
  mode = settings.get("mode", "?")
  cap = settings.get("max_spend_per_hour_usd", "?")
  exposure = bot.get("open_exposure_live_usd", bot.get("open_exposure_usd", 0))
  cash = kalshi.get("balance_usd")
  brti = kalshi.get("brti_live")
  open_n = bot.get("open_position_count", len(bot.get("open_positions") or []))

  print(f"\n=== {ts} | BTC hourly {mode.upper()} | cap ${cap} | at-risk ${exposure} | {open_n} bot legs ===")
  if brti is not None:
    print(f"    BRTI ${float(brti):,.2f} | Kalshi cash ${cash}")

  skip = bot.get("last_skip_reason")
  watch = bot.get("entry_watch") or {}
  if watch.get("signal"):
    print(f"    Watching: {watch.get('signal')} {watch.get('label') or ''}")
  if skip:
    print(f"    Skip: {skip}")

  if recon.get("ok"):
    print("    Reconcile: OK (bot and Kalshi match)")
  else:
    print("    Reconcile: MISMATCH")
    for row in recon.get("mismatches") or []:
      print(
        f"      COUNT: bot {_fmt_leg(row)} vs Kalshi x{row.get('kalshi_contracts')} "
        f"(delta {row.get('delta'):+d})"
      )
    for row in recon.get("bot_only") or []:
      sellable = row.get("kalshi_sellable")
      print(f"      BOT ONLY: {_fmt_leg(row)} | Kalshi sellable={sellable}")
    for row in recon.get("kalshi_only") or []:
      print(f"      KALSHI ONLY: {row.get('side', '?').upper()} {row.get('ticker')} x{row.get('contracts')}")
    for row in recon.get("orphan_resting_sells") or []:
      print(f"      ORPHAN SELL: {row.get('ticker')} order {row.get('order_id')}")

  trades = bot.get("hour_trades") or bot.get("recent_trades") or []
  for t in trades[:3]:
    if t.get("mode") != "live":
      continue
    print(
      f"    Last: {t.get('action')} {t.get('status')} {t.get('label') or t.get('market_ticker')} "
      f"— {(t.get('detail') or '')[:90]}"
    )
  return not recon.get("ok", True)


def main() -> int:
  parser = argparse.ArgumentParser(description="Monitor live bot vs Kalshi alignment")
  parser.add_argument("--base", default=DEFAULT_BASE)
  parser.add_argument("--watch", action="store_true", help="Poll every N seconds")
  parser.add_argument("--interval", type=float, default=15.0)
  args = parser.parse_args()

  pw = _password()
  if not pw:
    print("Set APP_PASSWORD in .env", file=sys.stderr)
    return 1

  session = requests.Session()
  _login(session, args.base, pw)

  while True:
    try:
      bot, recon, kalshi = _fetch(session, args.base)
      mismatch = _print_snapshot(bot, recon, kalshi)
      if mismatch:
        print("    >>> Action: check Kalshi Orders tab; cancel orphan sells if any.")
    except Exception as e:
      print(f"ERROR: {e}", file=sys.stderr)
      if not args.watch:
        return 1
    if not args.watch:
      break
    time.sleep(max(5.0, args.interval))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
