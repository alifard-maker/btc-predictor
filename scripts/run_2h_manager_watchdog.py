#!/usr/bin/env python3
"""Run the hourly S1/S2 manager monitor for a fixed wall-clock duration.

Restarts the monitor if the child exits early (crash, signal, OOM). Writes
data/logs/manager_monitor/watch_status.json so completion is always visible.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "data" / "logs" / "manager_monitor"
STATUS_PATH = LOG_DIR / "watch_status.json"
MONITOR = ROOT / "scripts" / "monitor_hourly_s1_s2_manager.py"


def _utc_iso() -> str:
  return datetime.now(timezone.utc).isoformat()


def _write_status(payload: dict) -> None:
  LOG_DIR.mkdir(parents=True, exist_ok=True)
  STATUS_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def main() -> int:
  duration_min = float(os.environ.get("MONITOR_DURATION_MINUTES", "120"))
  interval_s = float(os.environ.get("MONITOR_INTERVAL_SECONDS", "30"))
  started = time.time()
  deadline = started + duration_min * 60
  run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

  env = {
    **os.environ,
    "NO_PROXY": "*",
    "no_proxy": "*",
    "HTTP_PROXY": "",
    "HTTPS_PROXY": "",
    "ALL_PROXY": "",
    "PYTHONUNBUFFERED": "1",
  }

  _write_status(
    {
      "run_id": run_id,
      "state": "running",
      "started_at": _utc_iso(),
      "deadline_at": datetime.fromtimestamp(deadline, tz=timezone.utc).isoformat(),
      "duration_minutes": duration_min,
      "interval_seconds": interval_s,
      "restarts": 0,
      "last_child_exit": None,
    }
  )

  stdout_log = LOG_DIR / "monitor_stdout.log"
  restarts = 0

  while time.time() < deadline:
    remaining_min = max(0.1, (deadline - time.time()) / 60.0)
    cmd = [
      sys.executable,
      "-u",
      str(MONITOR),
      "--duration-minutes",
      f"{remaining_min:.2f}",
      "--interval",
      str(interval_s),
      "--lookback-hours",
      "2",
    ]
    print(f"[watchdog {run_id}] starting child remaining={remaining_min:.1f}m", flush=True)
    with stdout_log.open("a", encoding="utf-8") as out:
      out.write(f"\n[watchdog {run_id}] child start remaining={remaining_min:.1f}m at {_utc_iso()}\n")
      out.flush()
      proc = subprocess.run(cmd, cwd=ROOT, env=env, stdout=out, stderr=subprocess.STDOUT)

    if time.time() >= deadline:
      break
    restarts += 1
    _write_status(
      {
        "run_id": run_id,
        "state": "restarting",
        "started_at": datetime.fromtimestamp(started, tz=timezone.utc).isoformat(),
        "deadline_at": datetime.fromtimestamp(deadline, tz=timezone.utc).isoformat(),
        "duration_minutes": duration_min,
        "interval_seconds": interval_s,
        "restarts": restarts,
        "last_child_exit": proc.returncode,
        "updated_at": _utc_iso(),
      }
    )
    print(f"[watchdog {run_id}] child exited {proc.returncode}, restarting in 5s", flush=True)
    time.sleep(5)

  _write_status(
    {
      "run_id": run_id,
      "state": "complete",
      "started_at": datetime.fromtimestamp(started, tz=timezone.utc).isoformat(),
      "completed_at": _utc_iso(),
      "deadline_at": datetime.fromtimestamp(deadline, tz=timezone.utc).isoformat(),
      "duration_minutes": duration_min,
      "interval_seconds": interval_s,
      "restarts": restarts,
    }
  )
  with stdout_log.open("a", encoding="utf-8") as out:
    out.write(f"\n[watchdog {run_id}] COMPLETE at {_utc_iso()} restarts={restarts}\n")
  print(f"[watchdog {run_id}] COMPLETE restarts={restarts}", flush=True)
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
