#!/usr/bin/env python3
"""Track P&L-first Phase 0–1 pipeline milestone (gate-stack proof, not PnL fills)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
  sys.path.insert(0, str(ROOT))

DEFAULT_BASE = os.environ.get(
  "BTC_PREDICTOR_URL",
  "https://btc-predictor-production-f460.up.railway.app",
)
OUT_PATH = ROOT / "data" / "logs" / "pnl_first_milestone.json"


def _password() -> str:
  env_path = ROOT / ".env"
  if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
      if line.startswith("APP_PASSWORD="):
        return line.split("=", 1)[1].strip()
  return os.environ.get("APP_PASSWORD", "")


def _login(session: requests.Session, base: str) -> None:
  pw = _password()
  r = session.post(f"{base}/api/auth/login", data={"password": pw}, timeout=30)
  if r.status_code not in (200, 302, 303):
    raise RuntimeError(f"login failed: {r.status_code}")


def evaluate(session: requests.Session, base: str) -> dict:
  r = session.get(f"{base}/api/pnl-first/manager", timeout=60)
  body = r.json()
  return dict(body.get("milestone_now") or {})


def main() -> int:
  parser = argparse.ArgumentParser(description="P&L-first pipeline milestone tracker")
  parser.add_argument("--base", default=DEFAULT_BASE)
  args = parser.parse_args()

  session = requests.Session()
  _login(session, args.base)
  report = evaluate(session, args.base)
  OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
  OUT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")

  target = report.get("target_pipeline_hours") or report.get("target_positive_hours") or 20
  streak = report.get("consecutive_pipeline_hours") or report.get("consecutive_positive_hours") or 0
  missing = report.get("missing_session_gates") or []
  status = "ACHIEVED" if report.get("milestone_achieved") else "in progress"
  print(
    f"P&L-first pipeline milestone: {streak}/{target} live hours — {status}"
    + (f" (missing gates: {', '.join(missing)})" if missing else "")
  )
  return 0 if report.get("milestone_achieved") else 1


if __name__ == "__main__":
  raise SystemExit(main())
