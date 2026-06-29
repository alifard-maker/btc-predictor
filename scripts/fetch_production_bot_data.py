#!/usr/bin/env python3
"""Fetch bot trade / performance data from production dashboard session."""

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
TRADE_LIMIT = 200  # API max per request (main.py Query le=200)


def _password() -> str:
  env_path = Path(__file__).resolve().parents[1] / ".env"
  if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
      if line.startswith("APP_PASSWORD="):
        return line.split("=", 1)[1].strip()
  return os.environ.get("APP_PASSWORD", "")


def main() -> int:
  pw = _password()
  if not pw:
    print("Set APP_PASSWORD in .env", file=sys.stderr)
    return 1
  s = requests.Session()
  r = s.post(f"{BASE}/api/auth/login", data={"password": pw}, allow_redirects=False, timeout=30)
  if r.status_code not in (303, 302, 200):
    print("Login failed:", r.status_code, r.text[:200], file=sys.stderr)
    return 1
  out: dict = {}
  endpoints = (
    "/api/bots/performance-report",
    f"/api/hourly/bot/trades?limit={TRADE_LIMIT}",
    f"/api/eth/hourly/bot/trades?limit={TRADE_LIMIT}",
    f"/api/slot15/bot/trades?limit={TRADE_LIMIT}",
    f"/api/eth/15m/bot/trades?limit={TRADE_LIMIT}",
    "/api/hourly/bot",
    "/api/eth/hourly/bot",
    "/api/slot15/bot",
    "/api/eth/15m/bot",
  )
  for path in endpoints:
    resp = s.get(f"{BASE}{path}", timeout=60)
    if "trades" in path:
      key = path.split("/api/")[1].split("?")[0].replace("/", "_")
    else:
      key = path.split("?")[0].rstrip("/").split("/")[-1]
      if key == "performance-report":
        key = "performance_report"
    try:
      out[key] = resp.json()
    except Exception:
      out[key] = {"error": resp.status_code, "text": resp.text[:500]}
  print(json.dumps(out, indent=2))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
