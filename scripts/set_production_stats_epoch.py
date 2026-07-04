#!/usr/bin/env python3
"""Set stats_epoch_at on all production bot stores (session login)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import quote

import requests

BASE = os.environ.get(
  "BTC_PREDICTOR_URL",
  "https://btc-predictor-production-f460.up.railway.app",
)
DEFAULT_AT = "2026-07-04T16:59:00+00:00"  # Jul 4 2026 12:59 PM EDT


def _password() -> str:
  env_path = Path(__file__).resolve().parents[1] / ".env"
  if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
      if line.startswith("APP_PASSWORD="):
        return line.split("=", 1)[1].strip().strip('"').strip("'")
  return os.environ.get("APP_PASSWORD", "")


def main() -> int:
  at = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_AT
  pw = _password()
  if not pw:
    print("Set APP_PASSWORD in .env", file=sys.stderr)
    return 1
  s = requests.Session()
  r = s.post(f"{BASE}/api/auth/login", data={"password": pw}, allow_redirects=False, timeout=30)
  if r.status_code not in (303, 302, 200):
    print("Login failed:", r.status_code, r.text[:300], file=sys.stderr)
    return 1
  resp = s.post(
    f"{BASE}/api/admin/set-stats-epoch?at={quote(at, safe='')}",
    timeout=120,
  )
  if resp.status_code != 200:
    print("Set stats epoch failed:", resp.status_code, resp.text[:500], file=sys.stderr)
    return 1
  data = resp.json()
  print(f"Stats epoch set to {data.get('stats_epoch_at')} on {len(data.get('stores') or {})} stores")
  print(json.dumps(data, indent=2))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
