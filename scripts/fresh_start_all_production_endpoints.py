#!/usr/bin/env python3
"""Fresh-start every bot via per-card API routes (works before bulk admin upgrade)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests

BASE = os.environ.get(
  "BTC_PREDICTOR_URL",
  "https://btc-predictor-production-f460.up.railway.app",
)

FRESH_START_PATHS = (
  "/api/hourly/bot/fresh-start",
  "/api/hourly-trial/bot/fresh-start",
  "/api/hourly-trial-rally/bot/fresh-start",
  "/api/hourly-trial-soft/bot/fresh-start",
  "/api/hourly-trial-mech/bot/fresh-start",
  "/api/slot15/bot/fresh-start",
  "/api/eth/hourly/bot/fresh-start",
  "/api/eth/hourly-trial/bot/fresh-start",
  "/api/eth/15m/bot/fresh-start",
  "/api/eth/15m-trial/bot/fresh-start",
  "/api/hourly-v2/bot/fresh-start",
  "/api/eth/hourly-v2/bot/fresh-start",
  "/api/spx/hourly/bot/fresh-start",
  "/api/spx/hourly-trial/bot/fresh-start",
  "/api/ndx/hourly/bot/fresh-start",
  "/api/ndx/hourly-trial/bot/fresh-start",
)


def _password() -> str:
  env_path = Path(__file__).resolve().parents[1] / ".env"
  if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
      if line.startswith("APP_PASSWORD="):
        return line.split("=", 1)[1].strip().strip('"').strip("'")
  return os.environ.get("APP_PASSWORD", "")


def main() -> int:
  pw = _password()
  if not pw:
    print("Set APP_PASSWORD in .env", file=sys.stderr)
    return 1
  s = requests.Session()
  r = s.post(f"{BASE}/api/auth/login", data={"password": pw}, allow_redirects=False, timeout=30)
  if r.status_code not in (303, 302, 200):
    print("Login failed:", r.status_code, r.text[:300], file=sys.stderr)
    return 1

  results: dict[str, str] = {}
  ok = 0
  for path in FRESH_START_PATHS:
    resp = s.post(f"{BASE}{path}", timeout=90)
    key = path.replace("/api/", "").replace("/bot/fresh-start", "")
    if resp.status_code == 200:
      ok += 1
      mode = (resp.json() or {}).get("settings", {}).get("mode", "?")
      results[key] = f"ok ({mode})"
    else:
      results[key] = f"HTTP {resp.status_code}: {resp.text[:120]}"
  print(f"Fresh-started {ok}/{len(FRESH_START_PATHS)} bots on {BASE}")
  print(json.dumps(results, indent=2))
  return 0 if ok > 0 else 1


if __name__ == "__main__":
  raise SystemExit(main())
