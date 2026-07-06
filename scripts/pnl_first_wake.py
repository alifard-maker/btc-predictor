#!/usr/bin/env python3
"""Controlled wake for P&L-first Phase 0–1 — BTC hourly live only after preflight."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
BASE = os.environ.get(
  "BTC_PREDICTOR_URL",
  "https://btc-predictor-production-f460.up.railway.app",
)


def _password() -> str:
  env_path = ROOT / ".env"
  if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
      if line.startswith("APP_PASSWORD="):
        return line.split("=", 1)[1].strip().strip('"').strip("'")
  return os.environ.get("APP_PASSWORD", "")


def preflight(session: requests.Session, base: str) -> tuple[list[str], dict]:
  issues: list[str] = []
  detail: dict = {}

  health = session.get(f"{base}/health", timeout=20).json()
  detail["version"] = health.get("version")
  if health.get("status") != "ok":
    issues.append("health_not_ok")
  if not str(health.get("version") or "").startswith("Beta 5"):
    issues.append(f"version_not_5x:{health.get('version')}")

  tab = session.get(f"{base}/api/hourly", timeout=60).json()
  event = (tab.get("event") or {}).get("event_ticker")
  detail["event_ticker"] = event
  if not tab.get("ok") or not event:
    issues.append("hourly_tab_unavailable")

  bot = session.get(f"{base}/api/hourly/bot", timeout=60).json()
  detail["open_legs"] = len(bot.get("open_positions") or [])
  detail["open_exposure"] = bot.get("open_exposure_live_usd", bot.get("open_exposure_usd"))
  if detail["open_legs"] or float(detail["open_exposure"] or 0) > 0:
    issues.append("bot_has_open_legs")

  recon = session.get(f"{base}/api/hourly/bot/live-reconcile", timeout=60).json()
  detail["kalshi_only"] = len(recon.get("kalshi_only") or [])
  detail["bot_only"] = len(recon.get("bot_only") or [])
  detail["kalshi_contracts"] = recon.get("kalshi_contracts")
  if detail["bot_only"]:
    issues.append("reconcile_bot_only")
  if detail["kalshi_only"] and event and recon.get("event_ticker") == event:
    issues.append(f"reconcile_kalshi_only:{detail['kalshi_only']}")

  return issues, detail


def main() -> int:
  parser = argparse.ArgumentParser(description="P&L-first controlled BTC hourly live wake")
  parser.add_argument("--base", default=BASE)
  parser.add_argument("--cap-usd", type=float, default=30.0)
  parser.add_argument("--check-only", action="store_true")
  parser.add_argument("--live-password", default=os.environ.get("LIVE_BET_PASSWORD", ""))
  args = parser.parse_args()

  pw = _password()
  session = requests.Session()
  session.post(f"{args.base}/api/auth/login", data={"password": pw}, timeout=30)

  issues, detail = preflight(session, args.base)
  print("Preflight:", json.dumps({"issues": issues, "detail": detail}, indent=2))

  if args.check_only:
    return 1 if issues else 0

  if issues:
    print("BLOCKED — fix preflight issues before live wake", file=sys.stderr)
    return 1

  body = {
    "enabled": True,
    "mode": "live",
    "max_spend_per_hour_usd": args.cap_usd,
    "allow_strong": False,
    "allow_actionable": False,
  }
  if args.live_password:
    body["live_bet_password"] = args.live_password

  resp = session.post(f"{args.base}/api/hourly/bot/settings", json=body, timeout=60)
  if resp.status_code >= 400:
    print("Wake failed:", resp.status_code, resp.text[:300], file=sys.stderr)
    return 1

  out = resp.json()
  st = out.get("settings") or {}
  gs = out.get("live_entry_guard_summary") or {}
  print(
    f"BTC hourly LIVE wake: enabled={st.get('enabled')} cap=${st.get('max_spend_per_hour_usd')} "
    f"profile={gs.get('mechanics_profile')} skip={out.get('last_skip_reason')}"
  )
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
