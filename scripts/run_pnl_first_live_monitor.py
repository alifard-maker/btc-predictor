#!/usr/bin/env python3
"""Watchdog for pnl_first_live_monitor — restarts on crash, runs until stopped."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "data" / "logs" / "pnl_first_live_monitor"
STATUS_PATH = LOG_DIR / "watchdog_status.json"
MONITOR = ROOT / "scripts" / "pnl_first_live_monitor.py"
STDOUT_LOG = LOG_DIR / "monitor_stdout.log"


def main() -> int:
  interval = float(os.environ.get("PNL_FIRST_MONITOR_INTERVAL", "30"))
  run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
  restarts = 0
  LOG_DIR.mkdir(parents=True, exist_ok=True)
  env = {**os.environ, "PYTHONUNBUFFERED": "1"}

  STATUS_PATH.write_text(
    json.dumps({"state": "running", "run_id": run_id, "started_at": datetime.now(timezone.utc).isoformat()}, indent=2),
    encoding="utf-8",
  )
  print(f"[pnl_first watchdog {run_id}] ON interval={interval}s", flush=True)

  while True:
    cmd = [sys.executable, "-u", str(MONITOR), "--interval", str(interval)]
    with STDOUT_LOG.open("a", encoding="utf-8") as out:
      out.write(f"\n[watchdog {run_id}] child start restarts={restarts} at {datetime.now(timezone.utc).isoformat()}\n")
      out.flush()
      proc = subprocess.run(cmd, cwd=ROOT, env=env, stdout=out, stderr=subprocess.STDOUT)
    restarts += 1
    STATUS_PATH.write_text(
      json.dumps(
        {
          "state": "restarting",
          "run_id": run_id,
          "restarts": restarts,
          "last_exit": proc.returncode,
          "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        indent=2,
      ),
      encoding="utf-8",
    )
    print(f"[watchdog {run_id}] child exited {proc.returncode}, restart in 5s", flush=True)
    time.sleep(5)


if __name__ == "__main__":
  raise SystemExit(main())
