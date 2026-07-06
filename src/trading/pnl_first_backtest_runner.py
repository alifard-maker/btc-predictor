"""Railway background backtest queue + live P&L audit for P&L-first manager."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_RUN_LOCK = threading.Lock()
_ACTIVE_PROC: subprocess.Popen[str] | None = None
_ACTIVE_JOB: dict[str, Any] | None = None

DEFAULT_JOBS: list[dict[str, str]] = [
  {
    "id": "phase_a_structure_sweep_v2",
    "script": "scripts/backtest_structure_memory_sweep_v2.py",
    "output": "data/logs/backtest_structure_memory_sweep_v2.json",
    "milestone": "phase_a_fair_baseline_and_structure_tune",
  },
  {
    "id": "phase_b_walkforward_ml",
    "script": "scripts/backtest_pnl_first_walkforward.py",
    "output": "data/logs/backtest_pnl_first_walkforward.json",
    "milestone": "phase_b_walkforward_ml_baseline",
  },
]


def backtest_log_dir(cfg: dict[str, Any] | None = None) -> Path:
  del cfg
  base = Path(os.getenv("DATA_DIR", "data"))
  d = base / "logs" / "pnl_first_backtests"
  d.mkdir(parents=True, exist_ok=True)
  return d


def _project_root() -> Path:
  return Path(__file__).resolve().parents[2]


def ensure_backtest_queue(cfg: dict[str, Any] | None) -> list[dict[str, Any]]:
  from src.trading.pnl_first_railway_manager import load_manager_state, save_manager_state

  state = load_manager_state(cfg)
  jobs = state.get("backtest_jobs")
  if isinstance(jobs, list) and jobs:
    return jobs
  queued = []
  for j in DEFAULT_JOBS:
    queued.append({**j, "status": "pending", "queued_at": datetime.now(timezone.utc).isoformat()})
  state["backtest_jobs"] = queued
  state["backtest_queue_initialized_at"] = datetime.now(timezone.utc).isoformat()
  save_manager_state(state, cfg)
  log.info("pnl_first backtest queue initialized (%d jobs)", len(queued))
  return queued


def _persist_jobs(cfg: dict[str, Any] | None, jobs: list[dict[str, Any]]) -> None:
  from src.trading.pnl_first_railway_manager import load_manager_state, save_manager_state

  state = load_manager_state(cfg)
  state["backtest_jobs"] = jobs
  save_manager_state(state, cfg)


def _job_log_path(job_id: str) -> Path:
  return backtest_log_dir() / f"{job_id}.log"


def tick_backtest_runner(cfg: dict[str, Any] | None) -> dict[str, Any] | None:
  """Start or poll one background backtest job (non-blocking)."""
  global _ACTIVE_PROC, _ACTIVE_JOB
  jobs = ensure_backtest_queue(cfg)

  with _RUN_LOCK:
    if _ACTIVE_PROC is not None:
      rc = _ACTIVE_PROC.poll()
      if rc is None:
        return {"action": "backtest_running", "job": _ACTIVE_JOB}
      log_path = _job_log_path(str(_ACTIVE_JOB.get("id")))
      finished = datetime.now(timezone.utc).isoformat()
      for j in jobs:
        if j.get("id") == _ACTIVE_JOB.get("id"):
          j["status"] = "completed" if rc == 0 else "failed"
          j["exit_code"] = rc
          j["finished_at"] = finished
          j["log_path"] = str(log_path)
          out = _project_root() / str(j.get("output", ""))
          if out.exists():
            j["output_path"] = str(out)
            try:
              j["result_preview"] = json.loads(out.read_text(encoding="utf-8"))
            except Exception:
              pass
      _persist_jobs(cfg, jobs)
      _ACTIVE_PROC = None
      job_id = _ACTIVE_JOB.get("id")
      _ACTIVE_JOB = None
      log.info("pnl_first backtest job %s finished rc=%s", job_id, rc)
      return {"action": "backtest_finished", "job_id": job_id, "exit_code": rc}

    next_job = next((j for j in jobs if j.get("status") == "pending"), None)
    if not next_job:
      return {"action": "backtest_queue_idle", "completed": sum(1 for j in jobs if j.get("status") == "completed")}

    root = _project_root()
    script = root / str(next_job["script"])
    if not script.exists():
      next_job["status"] = "failed"
      next_job["error"] = f"missing script {script}"
      _persist_jobs(cfg, jobs)
      return {"action": "backtest_failed", "job": next_job}

    log_path = _job_log_path(str(next_job["id"]))
    next_job["status"] = "running"
    next_job["started_at"] = datetime.now(timezone.utc).isoformat()
    _persist_jobs(cfg, jobs)

    out_f = log_path.open("w", encoding="utf-8")
    _ACTIVE_PROC = subprocess.Popen(
      [sys.executable, "-u", str(script)],
      cwd=root,
      stdout=out_f,
      stderr=subprocess.STDOUT,
      env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    _ACTIVE_JOB = next_job
    log.info("pnl_first backtest started: %s", next_job["id"])
    return {"action": "backtest_started", "job": next_job}


def run_live_pnl_audit(loop: Any, cfg: dict[str, Any] | None) -> dict[str, Any]:
  """Kalshi vs bot hour P&L + periodic fill sync (runs on Railway manager)."""
  from src.trading.pnl_first_railway_manager import _stats_epoch

  audit: dict[str, Any] = {"ts": datetime.now(timezone.utc).isoformat(), "issues": []}
  tab = loop.daily_prediction()
  event = (tab.get("event") or {}).get("event_ticker") if tab.get("ok") else None
  audit["event_ticker"] = event

  store = loop.hourly_bot_store("btc")
  settings = store.get_settings()
  audit["btc_live"] = settings.mode == "live" and settings.enabled

  if event:
    tab_ok = tab if tab.get("ok") else None
    try:
      status = loop.hourly_bot_status("btc", tab_ok)
      hs = status.get("hour_summary") or {}
    except Exception:
      hs = {}
    bot_hr = float(hs.get("realized_pnl_usd") or 0)
    open_legs = len(store.open_positions(event) or [])
    audit["bot_hour_realized_usd"] = bot_hr
    audit["open_legs"] = open_legs
    audit["hour_partial"] = open_legs > 0

    kalshi = loop.kalshi
    if kalshi and getattr(kalshi, "authenticated", False):
      from src.trading.kalshi_fill_sync import summarize_kalshi_experiment_fills

      since = _stats_epoch(cfg)
      sm = summarize_kalshi_experiment_fills(
        kalshi, since=since, asset="btc", event_ticker=str(event),
      )
      kalshi_hr = float(sm.get("total_pnl_usd") or 0)
      audit["kalshi_hour_pnl_usd"] = kalshi_hr
      audit["kalshi_hour_closed"] = sm.get("closed_trades")
      if not audit["hour_partial"] and abs(bot_hr - kalshi_hr) > 0.12:
        audit["issues"].append({
          "code": "kalshi_hour_pnl_drift",
          "bot_usd": bot_hr,
          "kalshi_usd": kalshi_hr,
          "delta_usd": round(bot_hr - kalshi_hr, 2),
        })

  log_dir = backtest_log_dir(cfg)
  issues_path = log_dir / "live_audit_issues.jsonl"
  if audit.get("issues"):
    with issues_path.open("a", encoding="utf-8") as f:
      f.write(json.dumps(audit, default=str) + "\n")

  audit_path = log_dir / "live_audit_latest.json"
  audit_path.write_text(json.dumps(audit, indent=2, default=str), encoding="utf-8")
  return audit


def maybe_sync_kalshi_periodic(loop: Any, cycle: int, *, every: int = 15) -> dict[str, Any] | None:
  if cycle % every != 0:
    return None
  try:
    return loop.sync_hourly_kalshi_fills("btc", force=True)
  except Exception as exc:
    log.warning("periodic kalshi sync failed: %s", exc)
    return {"ok": False, "error": str(exc)}


def backtest_status(cfg: dict[str, Any] | None) -> dict[str, Any]:
  jobs = ensure_backtest_queue(cfg)
  with _RUN_LOCK:
    running = _ACTIVE_JOB
  return {
    "jobs": jobs,
    "running": running,
    "log_dir": str(backtest_log_dir(cfg)),
  }
