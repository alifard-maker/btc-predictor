#!/usr/bin/env python3
"""Lock all production bots to sleep (paper + auto-bet off)."""

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
SLEEP_BODY = {"enabled": False, "mode": "paper"}

HOURLY_SETTINGS = (
  "/api/hourly/bot/settings",
  "/api/eth/hourly/bot/settings",
  "/api/hourly-v2/bot/settings",
  "/api/eth/hourly-v2/bot/settings",
  "/api/hourly-trial/bot/settings",
  "/api/hourly-trial-mech/bot/settings",
  "/api/hourly-trial-rally/bot/settings",
  "/api/hourly-trial-soft/bot/settings",
  "/api/eth/hourly-trial/bot/settings",
  "/api/slot15/bot/settings",
  "/api/eth/15m/bot/settings",
  "/api/spx/hourly/bot/settings",
  "/api/spx/hourly-trial/bot/settings",
  "/api/ndx/hourly/bot/settings",
  "/api/ndx/hourly-trial/bot/settings",
)


def _password() -> str:
  env_path = ROOT / ".env"
  if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
      if line.startswith("APP_PASSWORD="):
        return line.split("=", 1)[1].strip().strip('"').strip("'")
  return os.environ.get("APP_PASSWORD", "")


def main() -> int:
  parser = argparse.ArgumentParser(description="Sleep all production bots")
  parser.add_argument("--base", default=BASE)
  parser.add_argument("--dry-run", action="store_true")
  args = parser.parse_args()

  pw = _password()
  if not pw:
    print("APP_PASSWORD required", file=sys.stderr)
    return 1

  session = requests.Session()
  r = session.post(f"{args.base}/api/auth/login", data={"password": pw}, timeout=30)
  if r.status_code not in (200, 302, 303):
    print("login failed", r.status_code, file=sys.stderr)
    return 1

  health = session.get(f"{args.base}/health", timeout=20).json()
  print(f"Production {health.get('version')} — locking sleep mode")

  results: dict[str, str] = {}
  for path in HOURLY_SETTINGS:
    if args.dry_run:
      results[path] = "dry-run"
      continue
    try:
      body = {"enabled": False} if path == "/api/hourly/bot/settings" else SLEEP_BODY
      resp = session.post(f"{args.base}{path}", json=body, timeout=45)
      if resp.status_code >= 400:
        results[path] = f"HTTP {resp.status_code}"
        continue
      body = resp.json()
      st = body.get("settings") or {}
      results[path] = f"enabled={st.get('enabled')} mode={st.get('mode')}"
    except Exception as exc:
      results[path] = f"error:{exc}"

  print(json.dumps(results, indent=2))
  bad = [p for p, v in results.items() if "enabled=True" in v or "mode=live" in v]
  return 1 if bad else 0


if __name__ == "__main__":
  raise SystemExit(main())
